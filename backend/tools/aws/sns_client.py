# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SNS client for sending critical finding notifications."""

import os
import logging
import boto3

logger = logging.getLogger("sns-client")


class NotificationService:
    """Sends notifications for critical database findings via SNS."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "sns",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
        return self._client

    @property
    def topic_arn(self):
        return os.getenv("NOTIFICATION_TOPIC_ARN", "")

    def notify_critical_findings(self, instance_name: str, instance_id: str,
                                  findings: list[dict], overall_score: str) -> bool:
        """Send an SNS notification for critical/warning findings.

        Args:
            instance_name: Human-readable database name
            instance_id: Database instance ID
            findings: List of finding dicts with area, severity, message
            overall_score: Overall health score (critical/warning/ok)

        Returns:
            True if notification sent, False otherwise
        """
        if not self.topic_arn:
            logger.debug("No NOTIFICATION_TOPIC_ARN configured, skipping notification")
            return False

        critical = [f for f in findings if f.get("severity") == "CRITICAL"]
        warnings = [f for f in findings if f.get("severity") == "WARNING"]

        if not critical and not warnings:
            return False

        # Build message
        subject = f"[{overall_score.upper()}] DB Health Alert: {instance_name}"
        if len(subject) > 100:
            subject = subject[:97] + "..."

        lines = [
            f"Database Health Alert for {instance_name}",
            f"Instance ID: {instance_id}",
            f"Overall Score: {overall_score.upper()}",
            "",
        ]

        if critical:
            lines.append(f"🔴 CRITICAL ({len(critical)}):")
            for f in critical:
                lines.append(f"  [{f['area']}] {f.get('detail', f.get('message', ''))}")
            lines.append("")

        if warnings:
            lines.append(f"🟡 WARNING ({len(warnings)}):")
            for f in warnings:
                lines.append(f"  [{f['area']}] {f.get('detail', f.get('message', ''))}")
            lines.append("")

        lines.append("Review the full report in the DB Optimization dashboard.")

        message = "\n".join(lines)

        try:
            self.client.publish(
                TopicArn=self.topic_arn,
                Subject=subject,
                Message=message,
                MessageAttributes={
                    "severity": {
                        "DataType": "String",
                        "StringValue": overall_score,
                    },
                    "instance_id": {
                        "DataType": "String",
                        "StringValue": instance_id,
                    },
                },
            )
            logger.info(f"Notification sent for {instance_name}: {overall_score}")
            return True
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return False


notification_service = NotificationService()
