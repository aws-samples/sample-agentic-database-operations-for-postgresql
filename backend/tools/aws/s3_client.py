# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""S3 client for storing and retrieving database health reports."""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import boto3
from tools.resource_names import reports_bucket

logger = logging.getLogger("s3-client")


class ReportStorage:
    """Manages health reports in S3."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from botocore.config import Config
            self._client = boto3.client(
                "s3",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
                config=Config(max_pool_connections=50),
            )
        return self._client

    @property
    def bucket(self):
        return reports_bucket()

    def _build_key(self, account_id: str, instance_id: str,
                   date_str: str = None) -> str:
        """Build S3 key: {account_id}/{instance_id}/{date}/report.json"""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{account_id}/{instance_id}/{date_str}/report.json"

    def store_report(self, account_id: str, instance_id: str,
                     report: dict) -> str:
        """Store a health report to S3. Returns the S3 key."""
        key = self._build_key(account_id, instance_id)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(report, default=str),
            ContentType="application/json",
        )
        logger.info(f"Report stored: s3://{self.bucket}/{key}")
        return key

    def get_report(self, key: str) -> Optional[dict]:
        """Retrieve a report from S3 by key."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"].read().decode("utf-8")
            if key.endswith(".html"):
                # Also try to load the analysis.json from the same directory
                analysis = None
                analysis_key = key.rsplit("/", 1)[0] + "/analysis.json"
                try:
                    a_resp = self.client.get_object(Bucket=self.bucket, Key=analysis_key)
                    analysis = json.loads(a_resp["Body"].read().decode("utf-8"))
                except Exception:
                    pass
                return {"html": body, "key": key, "analysis": analysis}
            return json.loads(body)
        except self.client.exceptions.NoSuchKey:
            return None

    def list_reports(self, account_id: str = None, instance_id: str = None,
                     date_from: str = None, date_to: str = None) -> list[dict]:
        """List available reports with optional filters."""
        prefix = ""
        if account_id:
            prefix = f"{account_id}/"
            if instance_id:
                prefix = f"{account_id}/{instance_id}/"

        paginator = self.client.get_paginator("list_objects_v2")
        reports = []

        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                parts = key.split("/")
                # Support: account/instance/date/report-name/report.html (5 parts)
                # and old: account/instance/date/report.html (4 parts)
                if len(parts) >= 4 and parts[-1] in ("report.json", "report.html", "analysis.json"):
                    report_account = parts[0]
                    report_instance = parts[1]
                    report_date = parts[2]
                    # Extract report_name from folder (4th part if 5 parts)
                    report_name = parts[3] if len(parts) >= 5 else ""

                    if instance_id and report_instance != instance_id:
                        continue
                    if date_from and report_date < date_from:
                        continue
                    if date_to and report_date > date_to:
                        continue
                    reports.append({
                        "key": key,
                        "account_id": report_account,
                        "instance_id": report_instance,
                        "date": report_date,
                        "report_name": report_name,
                        "type": "html" if parts[-1] == "report.html" else "json",
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    })

        return sorted(reports, key=lambda r: (r["date"], r.get("last_modified", "")), reverse=True)


report_storage = ReportStorage()
