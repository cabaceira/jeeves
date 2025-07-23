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

def session() -> boto3.Session:
    """
    Create and return a boto3 Session using AWS credentials
    and region configured in settings or environment.
    """
    return boto3.Session(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        aws_session_token=settings.aws_session_token,
        # boto3 will also fall back to AWS_DEFAULT_REGION if this is None
        region_name=getattr(settings, "region_name", None),
    )


def latest_ubuntu_ami(ec2_client, os_version: str | None = None) -> str:
    """
    Retrieve the most recent Ubuntu AMI ID for the specified OS version.

    Tries several name‐patterns (including codename mappings) to handle
    variations in the official Ubuntu AMI naming scheme.

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

    canonical_owner = "099720109477"
    # Build a list of name-patterns to try, most-specific first
    patterns: list[str] = []
    codename = _VERSION_CODENAME.get(os_version)
    if codename:
        patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{os_version}-{codename}-*server-*")
        patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{codename}-{os_version}-*server-*")
    # Generic version-first patterns
    patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{os_version}-*amd64-server-*")
    patterns.append(f"ubuntu/images/hvm-ssd/ubuntu-{os_version}-*server-*")
    # Last-resort fuzzy match
    patterns.append(f"*ubuntu*{os_version}*server*")

    # Common filters for all queries
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

    # Sort by CreationDate (ISO8601) and return the newest
    images.sort(key=lambda img: img["CreationDate"])
    return images[-1]["ImageId"]
