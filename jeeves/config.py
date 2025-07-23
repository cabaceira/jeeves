from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    aws_access_key_id: str | None = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_session_token: str | None = os.getenv("AWS_SESSION_TOKEN")
    region_name: str = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

    default_os_version: str = os.getenv("DEFAULT_OS_VERSION", "24.04")
    default_instance_type: str = os.getenv("DEFAULT_INSTANCE_TYPE", "t2.xlarge")

    domain: str = os.getenv("DOMAIN", "")
    letsencrypt_email: str = os.getenv("LETSENCRYPT_EMAIL", "")

    k8s_namespace: str = os.getenv("K8S_NAMESPACE", "rocketchat")
    worker_ha: bool = os.getenv("WORKER_HA", "false").lower() == "true"

settings = Settings()
