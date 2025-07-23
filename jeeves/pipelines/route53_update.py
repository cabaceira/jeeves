# jeeves/pipelines/route53_update.py

"""
Pipeline: route53_update

Create or update a Route 53 A record for your Rocket.Chat DOMAIN,
pointing it at the Rocket.Chat EC2 instance’s public IP.
"""

from __future__ import annotations
import sys
import pathlib
from botocore.exceptions import ClientError
from ..pipeline import Pipeline
from ..aws_helpers import session
from ..config import settings


class Route53Update(Pipeline):
    pipeline_name        = "Update Route53 SubDomain"
    pipeline_description = "Updates Route53 SubDomain A record. Creates if it does not exist"
    docs_path            = pathlib.Path(__file__).parents[2] / "docs" / "route53_update.md"
    def run(self) -> None:
        # 1) Read DOMAIN from settings
        domain = settings.domain.strip()
        if not domain:
            raise RuntimeError("DOMAIN must be set in .env and loaded into settings")

        # 2) Discover the Rocket.Chat EC2 instance by tag
        sess = session()
        ec2 = sess.resource("ec2")
        running = list(ec2.instances.filter(
            Filters=[
                {"Name": "tag:Name", "Values": ["jeeves-rocketchat"]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        ))
        if not running:
            raise RuntimeError("No running EC2 instance tagged 'jeeves-rocketchat' found")
        rc = running[0]
        public_ip = rc.public_ip_address
        if not public_ip:
            raise RuntimeError(f"Instance {rc.id} has no public IP")
        print(f"Rocket.Chat instance: {rc.id} → {public_ip}")

        # 3) Find the Hosted Zone for the domain’s parent zone
        #    e.g. for 'chat.example.com', we look up 'example.com.'
        if domain.count(".") < 1:
            raise RuntimeError(f"DOMAIN '{domain}' is not a valid subdomain")
        parent = ".".join(domain.split(".")[1:]) + "."
        r53 = sess.client("route53")

        try:
            zones_resp = r53.list_hosted_zones_by_name(DNSName=parent, MaxItems="1")
        except ClientError as e:
            raise RuntimeError(f"Error listing hosted zones: {e}") from e

        hz = zones_resp.get("HostedZones", [])
        if not hz or hz[0]["Name"] != parent:
            raise RuntimeError(f"No hosted zone matching '{parent}'")
        zone_id = hz[0]["Id"].split("/")[-1]
        print(f"Using hosted zone {hz[0]['Name']} (ID: {zone_id})")

        # 4) Prepare UPSERT for the A record
        change_batch = {
            "Comment": "Upsert by Jeeves route53_update pipeline",
            "Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": domain,
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": public_ip}],
                }
            }]
        }

        # 5) Submit the change
        try:
            resp = r53.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch=change_batch
            )
        except ClientError as e:
            raise RuntimeError(f"Failed to UPSERT record: {e}") from e

        info = resp.get("ChangeInfo", {})
        print(f"Change submitted: ID={info.get('Id')} Status={info.get('Status')}")

def run(**kwargs):
    Route53Update().run()
