import boto3
from .config import settings

# Map numeric Ubuntu version strings to codenames for fallback
_VERSION_CODENAME: dict[str, str] = {
    "24.04": "noble",
    "22.04": "jammy",
    "20.04": "focal",
    "18.04": "bionic",
    "16.04": "xenial",
}

# List of Ubuntu versions that are known to work with MongoDB
_SUPPORTED_UBUNTU_VERSIONS = {"22.04", "20.04", "18.04"}

def session() -> boto3.Session:
    """
    Create and return a boto3 Session using AWS credentials
    and region configured in settings or environment.
    """
    return boto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        aws_session_token=settings.aws_session_token,
        region_name=getattr(settings, "region_name", None),
    )


def latest_ubuntu_ami(ec2_client, os_version: str | None = None) -> str:
    """
    Retrieve the most recent Ubuntu AMI ID for the specified OS version.

    Falls back to 22.04 if an unsupported version like 24.04 is requested.

    Args:
        ec2_client: a boto3 EC2 client
        os_version: Ubuntu version string, e.g. "24.04". Falls back to
                    settings.default_os_version if None.

    Returns:
        The AMI ID (string) of the newest matching image.

    Raises:
        RuntimeError: if no matching AMIs are found after all patterns.
    """
    if os_version is None:
        os_version = settings.default_os_version

    # Fallback for MongoDB compatibility
    if os_version == "24.04":
        print("⚠️  WARNING: Ubuntu 24.04 (noble) is not yet supported by MongoDB. Falling back to 22.04 (jammy).")
        os_version = "22.04"

    if os_version not in _SUPPORTED_UBUNTU_VERSIONS:
        raise ValueError(
            f"Ubuntu version '{os_version}' is not supported for MongoDB deployments. "
            f"Supported versions: {', '.join(sorted(_SUPPORTED_UBUNTU_VERSIONS))}"
        )

    canonical_owner = "099720109477"
    codename = _VERSION_CODENAME.get(os_version)

    # Build list of name-patterns to try
    patterns: list[str] = []
    if codename:
        patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{os_version}-{codename}-*server-*")
        patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{codename}-{os_version}-*server-*")
    patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{os_version}-*amd64-server-*")
    patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{os_version}-*server-*")
    patterns.append(f"*ubuntu*{os_version}*server*")

    filters_common = [
        {"Name": "state",            "Values": ["available"]},
        {"Name": "architecture",     "Values": ["x86_64"]},
        {"Name": "root-device-type", "Values": ["ebs"]},
    ]

    images = []
    for name_pattern in patterns:
        resp = ec2_client.describe_images(
            Owners=[canonical_owner],
            Filters=[{"Name": "name", "Values": [name_pattern]}] + filters_common
        )
        images = resp.get("Images", [])
        if images:
            break

    if not images:
        tried = ", ".join(patterns)
        region = getattr(settings, "region_name", None) or boto3.Session().region_name
        raise RuntimeError(
            f"No Ubuntu AMIs found for version '{os_version}' in region '{region}'. "
            f"Patterns tried: {tried}"
        )

    images.sort(key=lambda img: img["CreationDate"])
    return images[-1]["ImageId"]
