#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Builds the three architecture diagrams in this folder from the AWS Architecture Icons.

    python3 docs/diagrams/build_diagrams.py --icons /path/to/extracted/Asset-Package

The icon package is downloaded from https://aws.amazon.com/architecture/icons/ (one zip,
extracted anywhere; it is not committed). The script writes

    overview.svg / .png                     one-glance picture for the README
    operator-app-architecture.svg / .png    per-hop detail, operator audience
    admin-console-architecture.svg / .png   per-hop detail, administrator audience

next to itself. The SVGs are self-contained (icons inlined, no external references) so
they render on GitHub; the PNGs are for the README, which is read where SVG may not be
(rsvg-convert from librsvg is used when present, otherwise only the SVGs are written).

Everything is placed by hand: nodes on a grid, arrows with explicit anchors and
waypoints. That is deliberate — an auto-layout would move things every time a node is
added, and a diagram people have learned to read should stay put.
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
INK = "#16191F"        # text
INK_2 = "#545B64"      # secondary text
LINE = "#545B64"       # arrows
STEP_BG = "#232F3E"    # numbered-step disc

# Group styling follows the official group icons: border colour, dash and the icon.
GROUPS = {
    "cloud":   dict(color="#242F3E", dash=None,  icon="group/AWS-Cloud_32"),
    "region":  dict(color="#00A4A6", dash="6 4", icon="group/Region_32"),
    "account": dict(color="#E7157B", dash=None,  icon="group/AWS-Account_32"),
    "vpc":     dict(color="#8C4FFF", dash=None,  icon="group/Virtual-private-cloud-VPC_32"),
    "private": dict(color="#00A4A6", dash=None,  icon="group/Private-subnet_32", fill="#F2F6E9"),
    "plain":   dict(color="#7D8998", dash="4 3",  icon=None),
}

# Short names → files in the icon package.
ICONS = {
    "cloudfront": "Arch_Networking-Content-Delivery/64/Arch_Amazon-CloudFront_64.svg",
    "waf": "Arch_Security-Identity/64/Arch_AWS-WAF_64.svg",
    "cognito": "Arch_Security-Identity/64/Arch_Amazon-Cognito_64.svg",
    "apigw": "Arch_Networking-Content-Delivery/64/Arch_Amazon-API-Gateway_64.svg",
    "alb": "Arch_Networking-Content-Delivery/64/Arch_Elastic-Load-Balancing_64.svg",
    "ecs": "Arch_Containers/64/Arch_Amazon-Elastic-Container-Service_64.svg",
    "fargate": "Arch_Containers/64/Arch_AWS-Fargate_64.svg",
    "batch": "Arch_Compute/64/Arch_AWS-Batch_64.svg",
    "lambda": "Arch_Compute/64/Arch_AWS-Lambda_64.svg",
    "agentcore": "Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock-AgentCore_64.svg",
    "bedrock": "Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock_64.svg",
    "dynamodb": "Arch_Databases/64/Arch_Amazon-DynamoDB_64.svg",
    "aurora": "Arch_Databases/64/Arch_Amazon-Aurora_64.svg",
    "rds": "Arch_Databases/64/Arch_Amazon-RDS_64.svg",
    "s3": "Arch_Storage/64/Arch_Amazon-Simple-Storage-Service_64.svg",
    "sns": "Arch_Application-Integration/64/Arch_Amazon-Simple-Notification-Service_64.svg",
    "eventbridge": "Arch_Application-Integration/64/Arch_Amazon-EventBridge_64.svg",
    "secrets": "Arch_Security-Identity/64/Arch_AWS-Secrets-Manager_64.svg",
    "iam": "Arch_Security-Identity/64/Arch_AWS-Identity-and-Access-Management_64.svg",
    "cloudwatch": "Arch_Management-Tools/64/Arch_Amazon-CloudWatch_64.svg",
    "cloudtrail": "Arch_Management-Tools/64/Arch_AWS-CloudTrail_64.svg",
    "cloudformation": "Arch_Management-Tools/64/Arch_AWS-CloudFormation_64.svg",
    "budgets": "Arch_Cloud-Financial-Management/64/Arch_AWS-Budgets_64.svg",
    "ses": "Arch_Business-Applications/64/Arch_Amazon-Simple-Email-Service_64.svg",
    "privatelink": "Arch_Networking-Content-Delivery/64/Arch_AWS-PrivateLink_64.svg",
    "vpc": "Arch_Networking-Content-Delivery/64/Arch_Amazon-Virtual-Private-Cloud_64.svg",
    "users": "res/Res_General-Icons/Res_48_Light/Res_Users_48_Light.svg",
    "user": "res/Res_General-Icons/Res_48_Light/Res_User_48_Light.svg",
    "postgres": "res/Res_Databases/Res_Amazon-Aurora-PostgreSQL-Instance_48.svg",
    "group/AWS-Cloud_32": "group/AWS-Cloud_32.svg",
    "group/Region_32": "group/Region_32.svg",
    "group/AWS-Account_32": "group/AWS-Account_32.svg",
    "group/Virtual-private-cloud-VPC_32": "group/Virtual-private-cloud-VPC_32.svg",
    "group/Private-subnet_32": "group/Private-subnet_32.svg",
}


class Icons:
    """Loads icon SVGs from the extracted package and inlines them as nested <svg>."""

    def __init__(self, root: Path):
        self.root = root
        self.dirs = {
            "arch": next(root.glob("Architecture-Service-Icons_*")),
            "group": next(root.glob("Architecture-Group-Icons_*")),
            "res": next(root.glob("Resource-Icons_*")),
        }
        self.cache: dict[str, tuple[str, str]] = {}

    def _path(self, rel: str) -> Path:
        if rel.startswith("group/"):
            return self.dirs["group"] / rel[len("group/"):]
        if rel.startswith("res/"):
            return self.dirs["res"] / rel[len("res/"):]
        return self.dirs["arch"] / rel

    def load(self, name: str) -> tuple[str, str]:
        if name in self.cache:
            return self.cache[name]
        text = self._path(ICONS[name]).read_text(encoding="utf-8")
        view = re.search(r'viewBox="([^"]+)"', text).group(1)
        inner = text[text.index(">", text.index("<svg")) + 1: text.rindex("</svg>")]
        inner = re.sub(r"<\?xml[^>]*\?>", "", inner)
        inner = re.sub(r"<title>.*?</title>", "", inner, flags=re.S)
        inner = re.sub(r'\s+id="[^"]*"', "", inner)   # ids would collide across icons
        self.cache[name] = (view, inner.strip())
        return self.cache[name]

    def place(self, name: str, x: float, y: float, size: float) -> str:
        view, inner = self.load(name)
        return (f'<svg x="{x:.1f}" y="{y:.1f}" width="{size}" height="{size}" '
                f'viewBox="{view}">{inner}</svg>')


def esc(s: str) -> str:
    return html.escape(s, quote=True)


class Diagram:
    def __init__(self, icons: Icons, width: int, height: int, title: str, subtitle: str):
        self.icons, self.w, self.h = icons, width, height
        self.layers = {"groups": [], "arrows": [], "nodes": [], "text": []}
        self.nodes: dict[str, dict] = {}
        self.text(24, 40, title, size=22, weight="700")
        self.text(24, 64, subtitle, size=13, color=INK_2)

    # ── primitives ──────────────────────────────────────────────────────────────
    def text(self, x, y, s, size=12, color=INK, weight="400", anchor="start", layer="text", italic=False, halo=False):
        style = f'font-family="{FONT}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}"'
        if italic:
            style += ' font-style="italic"'
        if halo:
            # A white outline painted under the glyphs, so a label can sit on top of a
            # line or a group border and stay readable.
            style += ' stroke="#FFFFFF" stroke-width="4" stroke-linejoin="round" paint-order="stroke"'
        self.layers[layer].append(f'<text x="{x:.1f}" y="{y:.1f}" {style}>{esc(s)}</text>')

    def group(self, x, y, w, h, kind, label, sublabel=None):
        g = GROUPS[kind]
        dash = f' stroke-dasharray="{g["dash"]}"' if g["dash"] else ""
        fill = g.get("fill", "none")
        self.layers["groups"].append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{fill}" '
            f'stroke="{g["color"]}" stroke-width="1.5"{dash}/>')
        tx = x + 10
        if g["icon"]:
            self.layers["groups"].append(self.icons.place(g["icon"], x + 1, y + 1, 26))
            tx = x + 34
        self.text(tx, y + 19, label, size=12.5, weight="700", color=g["color"] if kind != "cloud" else INK, layer="groups")
        if sublabel:
            self.text(tx, y + 34, sublabel, size=10.5, color=INK_2, layer="groups")

    def node(self, nid, icon, cx, cy, label, sub=None, size=56, label_w=150):
        """An icon centred at (cx, cy) with its label below. Anchors for arrows are the
        icon's box edges."""
        x, y = cx - size / 2, cy - size / 2
        self.layers["nodes"].append(self.icons.place(icon, x, y, size))
        ly = cy + size / 2 + 15
        for i, line in enumerate(label.split("\n")):
            self.text(cx, ly + i * 14, line, size=11.5, weight="700", anchor="middle", layer="nodes")
        if sub:
            base = ly + 14 * len(label.split("\n"))
            for i, line in enumerate(sub.split("\n")):
                self.text(cx, base + i * 12.5, line, size=10, color=INK_2, anchor="middle", layer="nodes")
        label_h = 15 + 14 * (len(label.split("\n")) - 1) + (12.5 * len(sub.split("\n")) if sub else 0)
        self.nodes[nid] = dict(cx=cx, cy=cy, size=size, label_h=label_h)

    def anchor(self, nid, side):
        """Arrow attachment points: the icon's sides and top, and — because the label
        sits under the icon — a bottom point below the label, so a line leaving or
        entering from below never runs through the text."""
        n = self.nodes[nid]
        h = n["size"] / 2 + 4
        return {"l": (n["cx"] - h, n["cy"]), "r": (n["cx"] + h, n["cy"]),
                "t": (n["cx"], n["cy"] - h), "b": (n["cx"], n["cy"] + h + n["label_h"] + 2)}[side]

    def arrow(self, src, dst, via=(), both=False, dashed=False, color=LINE, label=None, label_at=0.5, label_dy=-6, step=None, step_at=0.5):
        """src/dst are 'node:side' or (x, y). `via` lists elbow points."""
        def pt(p):
            if isinstance(p, str):
                nid, side = p.split(":")
                return self.anchor(nid, side)
            return p
        pts = [pt(src), *via, pt(dst)]
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        start = ' marker-start="url(#arrow-rev)"' if both else ""
        self.layers["arrows"].append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.6"{dash} marker-end="url(#arrow)"{start}/>')
        if label or step is not None:
            # Position along the polyline by fraction of total length.
            def along(frac):
                segs = [((pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1])) for i in range(len(pts) - 1)]
                total = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in segs)
                want = frac * total
                for a, b in segs:
                    L = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
                    if want <= L or (a, b) == segs[-1]:
                        t = 0 if L == 0 else min(1, want / L)
                        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    want -= L
                return pts[-1]
            if label:
                lx, ly = along(label_at)
                self.text(lx, ly + label_dy, label, size=10, color=INK_2, anchor="middle")
            if step is not None:
                sx, sy = along(step_at)
                self.step(step, sx, sy)

    def step(self, n, x, y):
        self.layers["text"].append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="{STEP_BG}" stroke="#FFFFFF" stroke-width="1.5"/>'
            f'<text x="{x:.1f}" y="{y + 4:.1f}" font-family="{FONT}" font-size="11" font-weight="700" '
            f'fill="#FFFFFF" text-anchor="middle">{n}</text>')

    def note(self, x, y, lines, size=10.5, color=INK_2):
        for i, line in enumerate(lines):
            self.text(x, y + i * 14, line, size=size, color=color)

    def steps_panel(self, x, y, w, items, cols=2, title="How it works"):
        """Numbered legend along the bottom, laid out in columns."""
        self.text(x, y, title, size=13, weight="700")
        col_w = w / cols
        per = -(-len(items) // cols)
        for c in range(cols):
            cy = y + 24
            for n, txt in items[c * per:(c + 1) * per]:
                cx = x + c * col_w
                self.step(n, cx + 10, cy - 4)
                lines = wrap(txt, int(col_w / 5.6))
                for j, line in enumerate(lines):
                    self.text(cx + 28, cy + j * 13, line, size=10.5)
                cy += 13 * len(lines) + 12

    def render(self) -> str:
        defs = ('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="{LINE}"/></marker>'
                '<marker id="arrow-rev" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                f'<path d="M10,0 L0,5 L10,10 z" fill="{LINE}"/></marker></defs>')
        body = "".join("".join(self.layers[k]) for k in ("groups", "arrows", "nodes", "text"))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
                f'font-family="{FONT}"><rect width="100%" height="100%" fill="#FFFFFF"/>{defs}{body}</svg>')


def wrap(s: str, width: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
# Overview: the whole product in one glance — who uses it, the three bands of the
# deployment (front door, application, data) and the read-only path to your databases.
# Deliberately coarse; the two diagrams below carry the per-hop detail.
# ══════════════════════════════════════════════════════════════════════════════
def overview(icons: Icons) -> Diagram:
    d = Diagram(icons, 1550, 830, "DBOps Agent — Overview",
                "An AI operations agent for Aurora and RDS PostgreSQL: web apps, a REST API, Strands agents on Bedrock, and a read-only path to your databases")

    # People
    d.node("adm", "user", 90, 260, "Administrators", "spaces · onboarding\nusers · governance")
    d.node("op", "users", 90, 455, "Operators", "DBAs · chat, dashboard\ninvestigations, trends")

    d.group(190, 95, 1330, 700, "cloud", "AWS Cloud")
    d.group(215, 135, 805, 635, "account", "Agent account", "one CloudFormation stack in your AWS account")

    # Band 1 — front door
    d.group(240, 178, 760, 187, "plain", "Front door", "static web apps, sign-in, authorised REST")
    d.node("cf", "cloudfront", 350, 260, "Amazon CloudFront", "operator app\nadmin console")
    d.node("cog", "cognito", 510, 260, "Amazon Cognito", "sign-in · MFA (TOTP)\noperators · admins")
    d.node("apigw", "apigw", 660, 260, "Amazon API Gateway", "HTTP API · /api/*\nJWT authorizer")

    # Band 2 — application
    d.group(240, 383, 760, 187, "plain", "Application", "the REST API, the agents, and the collectors that run on a schedule")
    d.node("agentcore", "agentcore", 350, 455, "Bedrock AgentCore", "Strands agents · streamed turns\nOrchestrator (chat) · Investigation\n→ Observability · PostgreSQL · Research")
    d.node("bedrock", "bedrock", 510, 455, "Amazon Bedrock", "Claude · optional\nGuardrails")
    d.node("ecs", "ecs", 660, 455, "ECS Fargate", "FastAPI REST API\nLearning agent after scans")
    d.node("sched", "eventbridge", 815, 455, "EventBridge Scheduler", "Tier 1 every 5 min\nTier 2 deep scan daily", size=48)
    d.node("collect", "batch", 940, 455, "Lambda · AWS Batch", "Tier 1 collector\nTier 2 deep scan", size=48)

    # Band 3 — data
    d.group(240, 588, 760, 162, "plain", "Data stores and alarms")
    d.node("ddb", "dynamodb", 350, 660, "Amazon DynamoDB", "spaces · databases · users\ninvestigations · audit", size=48)
    d.node("s3", "s3", 510, 660, "Amazon S3", "scan reports\nknowledge documents", size=48)
    d.node("store", "cloudwatch", 660, 660, "Metrics store", "DynamoDB records\nCloudWatch series", size=48)
    d.node("alarms", "sns", 880, 660, "Alarms → email", "CloudWatch alarms per database\nEventBridge → notifier · SES / SNS", size=48)

    # Between the accounts
    d.node("plink", "privatelink", 1075, 455, "VPC peering /\nPrivateLink", size=40)

    # Your database account(s)
    d.group(1135, 135, 360, 635, "account", "Your database account(s)", "can be the agent account")
    d.node("role", "iam", 1200, 260, "IAM role", "created by you\nread-only · ExternalId", size=48)
    d.node("sec", "secrets", 1315, 260, "Secrets Manager", "read-only DB user", size=48)
    d.node("tel", "cloudwatch", 1430, 260, "Telemetry", "CloudWatch · CloudTrail\nPerformance Insights", size=48)
    d.group(1160, 383, 310, 187, "vpc", "Database VPC")
    d.node("db", "postgres", 1240, 455, "Aurora PostgreSQL", "writer + readers", size=52)
    d.node("rdsdb", "rds", 1390, 455, "RDS PostgreSQL", "instances", size=52)
    d.note(1165, 640, ["Nothing is written to your databases:", "every agent tool, collector and scan", "runs read-only SQL as a least-", "privilege user, over private", "networking."], size=10.5)

    # ── flows ──
    d.arrow("adm:r", "cf:l", label="browser · HTTPS", label_at=0.72, label_dy=-8)
    d.arrow("op:r", "cf:l", via=[(180, 455), (180, 260)])
    d.arrow("op:r", "agentcore:l")
    d.text(270, 434, "chat & investigation", size=10, color=INK_2, anchor="middle", halo=True)
    d.text(270, 447, "turns · streamed", size=10, color=INK_2, anchor="middle", halo=True)
    d.arrow("cf:r", "cog:l", dashed=True, label="sign in", label_dy=14)
    d.arrow("cog:r", "apigw:l", label="REST · JWT", label_dy=-8)
    d.arrow("apigw:b", "ecs:t")
    d.text(670, 376, "VPC Link · internal ALB", size=10, color=INK_2)
    d.arrow("agentcore:r", "bedrock:l", label="Claude", label_dy=-8)
    # data: agents and the API read and write the three stores; collectors fill the metrics store
    d.arrow("agentcore:b", "ddb:t")
    d.arrow("agentcore:b", "s3:t", via=[(350, 578), (510, 578)])
    d.arrow("ecs:b", "store:t")
    d.arrow("ecs:b", "s3:t", via=[(660, 578), (510, 578)])
    d.arrow("sched:r", "collect:l", dashed=True, label="runs", label_dy=-8)
    d.arrow("collect:b", "store:t", via=[(940, 578), (660, 578)])
    d.arrow("store:r", "alarms:l", dashed=True, label="state change", label_dy=-8)
    # read-only path to the databases, and the assumed role for telemetry and credentials
    d.arrow((1000, 455), "plink:l")
    d.text(1075, 425, "read-only SQL · 5432", size=10, color=INK_2, anchor="middle")
    d.arrow("plink:r", (1160, 455))
    d.arrow((1000, 405), "role:l", via=[(1105, 405), (1105, 260)])
    d.text(1098, 330, "assume the", size=10, color=INK_2, anchor="end")
    d.text(1098, 343, "read-only role", size=10, color=INK_2, anchor="end")
    d.arrow("role:r", "sec:l", label="credentials", label_dy=14)
    d.arrow("role:b", "tel:b", via=[(1200, 352), (1430, 352)], dashed=True, label="metrics · load · change history", label_at=0.5, label_dy=14)

    d.note(1110, 815, ["Diagram uses the AWS Architecture Icons (aws.amazon.com/architecture/icons)."], size=9.5)
    return d


# ══════════════════════════════════════════════════════════════════════════════
# Operator app (the agent): sign-in, REST, streamed agent turns, scans, learning.
# ══════════════════════════════════════════════════════════════════════════════
def operator_app(icons: Icons) -> Diagram:
    d = Diagram(icons, 1760, 1360, "DBOps Agent — Operator App",
                "How an operator's chat, investigations, dashboard and scans reach the databases, and what stores the results")

    d.node("op", "users", 90, 380, "Operator", "browser · SPA")

    d.group(190, 90, 1555, 1030, "cloud", "AWS Cloud")
    d.group(215, 130, 1000, 960, "account", "Agent account", "this deployment · one CloudFormation root stack")
    d.group(240, 175, 950, 880, "region", "Region")

    # Edge + identity
    d.node("waf", "waf", 340, 265, "AWS WAF", "on by default · any Region\n(us-east-1 edge stack elsewhere)")
    d.node("cf", "cloudfront", 480, 265, "Amazon CloudFront", "operator app (SPA)\nS3 origin bucket")
    d.node("cog", "cognito", 640, 265, "Amazon Cognito", "user pool · MFA (TOTP)\ngroups: operators, admins")

    # Left column
    d.node("apigw", "apigw", 340, 515, "Amazon API Gateway", "HTTP API · /api/*\nJWT authorizer")
    d.node("ddb", "dynamodb", 340, 745, "Amazon DynamoDB", "12 tables: registries,\naudit, tier1-metrics, tier2-scans")
    d.node("s3", "s3", 480, 745, "Amazon S3", "scan reports\nknowledge documents")

    # VPC with the workloads
    d.group(590, 400, 470, 500, "vpc", "Agent VPC", "15 VPC endpoints · NAT only for cross-region calls")
    d.group(620, 440, 415, 190, "private", "Private subnets · API and agents")
    d.node("alb", "alb", 690, 515, "Internal ALB", "VPC Link target")
    d.node("ecs", "ecs", 830, 515, "ECS Fargate", "FastAPI REST API\n108 routes")
    d.node("agentcore", "agentcore", 975, 515, "Bedrock AgentCore", "Runtime · /invocations\nMemory · Gateway")
    d.group(620, 665, 415, 205, "private", "Private subnets · collection")
    d.node("batch", "batch", 690, 745, "AWS Batch (Fargate)", "Tier 2 deep scan\ndaily per database")
    d.node("tier1", "lambda", 830, 745, "AWS Lambda", "Tier 1 collector\nevery 5 min per database")
    d.node("store", "cloudwatch", 975, 745, "Metrics store", "tier1-metrics · tier2-scans\n+ CloudWatch series")

    # Right column (regional services) and the bottom row
    d.node("bedrock", "bedrock", 1130, 475, "Amazon Bedrock", "Claude · Guardrails (optional)", size=48)
    d.node("plink", "privatelink", 1160, 595, "VPC peering / PrivateLink", "peering by default,\nPrivateLink when CIDRs overlap", size=40)
    d.node("sched", "eventbridge", 1130, 745, "EventBridge Scheduler", "Tier 1 / Tier 2 schedules\nTier 2 via a scan-trigger Lambda", size=48)
    d.node("learn", "lambda", 830, 960, "AWS Lambda", "learning trigger\nM2M token → POST /api/learn")
    d.node("sns", "sns", 975, 960, "Amazon SNS", "scan complete ·\nalarm email (plain text)")
    d.node("notifier", "lambda", 1130, 960, "AWS Lambda", "alarm notifier\nEventBridge rule → SES / SNS", size=48)

    # Database account(s)
    d.group(1240, 130, 490, 960, "account", "Database account(s)", "one per connected account · can be the agent account")
    d.node("role", "iam", 1300, 265, "IAM role", "read-only, created by you\nExternalId bound to the space", size=48)
    d.node("sec", "secrets", 1420, 265, "Secrets Manager", "read-only DB user\ncredentials", size=48)
    d.node("cw", "cloudwatch", 1540, 265, "CloudWatch", "metrics · logs", size=48)
    d.node("ct", "cloudtrail", 1660, 265, "CloudTrail", "who changed what", size=48)
    d.group(1265, 400, 440, 280, "vpc", "Database VPC")
    d.node("db", "postgres", 1400, 530, "Aurora PostgreSQL", "writer + readers", size=52)
    d.node("rdsdb", "rds", 1570, 530, "RDS PostgreSQL", "instances", size=52)
    d.node("pi", "rds", 1330, 770, "Performance Insights", "database load · top SQL", size=48)

    # ── flows ──
    d.arrow("op:t", "waf:l", via=[(90, 265)], step=1, step_at=0.35)
    d.arrow("waf:r", "cf:l")
    d.arrow("cf:r", "cog:l", dashed=True, label="sign in · MFA", label_dy=14)
    d.arrow("op:b", "apigw:l", via=[(90, 515)], step=2, step_at=0.75)
    d.arrow("op:r", "agentcore:t", via=[(975, 380)], step=3, step_at=0.5,
            label="chat & investigation turns · NDJSON stream", label_at=0.7, label_dy=-8)
    d.arrow("apigw:r", "alb:l", label="VPC Link", label_dy=-8)
    d.arrow("alb:r", "ecs:l")
    d.arrow("agentcore:r", "bedrock:l", via=[(1050, 515), (1050, 475)], step=4, step_at=0.15)
    # data bus between the two subnet rows
    d.arrow("ecs:b", "ddb:t", via=[(830, 645), (340, 645)])
    d.arrow("agentcore:b", "s3:t", via=[(975, 645), (480, 645)], label="registries · reports · knowledge", label_at=0.72, label_dy=-6)
    d.arrow("agentcore:b", "store:t")
    # collection
    d.arrow("sched:t", "batch:t", via=[(1130, 690), (690, 690)], step=6, step_at=0.12, label="cadence per database", label_at=0.55, label_dy=-6)
    d.arrow((1130, 690), "tier1:t", via=[(830, 690)])
    d.arrow("tier1:r", "store:l", label="write", label_dy=-8)
    d.arrow("batch:b", "store:b", via=[(690, 845), (975, 845)], dashed=True, label="structured scan data", label_at=0.5, label_dy=14)
    d.arrow("batch:l", "sns:t", via=[(607, 745), (607, 915), (975, 915)], label="HTML report → S3, then notify", label_at=0.75, label_dy=-6)
    d.arrow("sns:l", "learn:r", step=7, step_at=0.5)
    # database alarms: a CloudWatch alarm state change reaches the notifier through EventBridge
    d.arrow("store:b", "notifier:t", via=[(975, 880), (1130, 880)], dashed=True, step=8, step_at=0.2, label="alarm state change", label_at=0.72, label_dy=-6)
    d.arrow("notifier:l", "sns:r", dashed=True)
    d.arrow("learn:l", "apigw:l", via=[(250, 960), (250, 515)], label="Learning agent runs in the API", label_at=0.35, label_dy=-6)
    # cross-account
    d.arrow((990, 483), "role:l", via=[(990, 420), (1225, 420), (1225, 265)], step=5, step_at=0.72,
            label="specialists assume the space's role", label_at=0.3, label_dy=-6)
    d.arrow("role:r", "sec:l", label="credentials", label_dy=-8)
    d.arrow("role:b", "ct:b", via=[(1300, 362), (1660, 362)], dashed=True, label="metrics · logs · change history", label_at=0.55, label_dy=14)
    d.arrow((1540, 362), "cw:b", dashed=True)
    d.arrow((1300, 362), "pi:l", via=[(1250, 362), (1250, 770)], dashed=True)
    d.arrow((1060, 595), (1265, 595))
    d.text(1068, 552, "read-only SQL · 5432", size=10, color=INK_2)
    d.text(1068, 566, "from agents, collector, scans", size=10, color=INK_2)

    d.steps_panel(24, 1160, 1720, [
        (1, "The operator signs in with Amazon Cognito (TOTP required) and loads the operator app from CloudFront, behind AWS WAF (on by default; created in a us-east-1 edge stack when the deployment is elsewhere)."),
        (2, "REST calls (dashboard, investigations, trends, topology, reports, sessions) go through API Gateway with a JWT authorizer, over a VPC Link to the internal ALB and the FastAPI service on ECS Fargate."),
        (3, "Chat and investigation turns stream straight to the Bedrock AgentCore Runtime (/invocations, JWT-authorised); the orchestrator or the investigation agent runs there and conversations persist in AgentCore Memory."),
        (4, "Agents call Claude on Amazon Bedrock, with the optional Bedrock Guardrails screening input and output; the research agent's web search goes through the AgentCore Gateway."),
        (5, "Specialists assume a read-only, ExternalId-bound role in the database account for CloudWatch, Performance Insights and CloudTrail, fetch the read-only user's credentials from Secrets Manager, and connect to PostgreSQL over VPC peering or PrivateLink."),
        (6, "EventBridge Scheduler runs the Tier 1 collector (Lambda, every 5 minutes) and the Tier 2 deep scan (AWS Batch) per database; both write their records to the DynamoDB metrics tables and publish the numerics to CloudWatch, and the scan's HTML report lands in S3."),
        (7, "A finished scan notifies SNS; a Lambda calls POST /api/learn with a machine token, and the Learning agent compares the scan against history and raises or resolves recommendations in DynamoDB."),
        (8, "The API keeps one CloudWatch alarm per database per enabled alarm on the Tier 1 series (collection failing, XID wraparound, connection saturation, long transactions, lock contention, deadlocks, replica lag, cache hit). A state change goes through an EventBridge rule to the alarm notifier, which emails the notification address as HTML over SES, or plain text over SNS."),
    ], cols=2)
    d.note(1290, 1345, ["Diagram uses the AWS Architecture Icons (aws.amazon.com/architecture/icons)."], size=9.5)
    return d


# ══════════════════════════════════════════════════════════════════════════════
# Admin console: spaces, accounts, discovery, network, monitoring, users, settings.
# ══════════════════════════════════════════════════════════════════════════════
def admin_console(icons: Icons) -> Diagram:
    d = Diagram(icons, 1760, 1200, "DBOps Agent — Admin Console",
                "How an administrator connects accounts, onboards databases, schedules collection and governs users, models and knowledge")

    d.node("adm", "user", 90, 380, "Administrator", "super admin or\nspace admin · browser")

    d.group(190, 90, 1555, 830, "cloud", "AWS Cloud")
    d.group(215, 130, 1000, 760, "account", "Agent account", "this deployment · one CloudFormation root stack")
    d.group(240, 175, 950, 680, "region", "Region")

    # Edge + identity + the services Settings governs
    d.node("cf", "cloudfront", 340, 265, "Amazon CloudFront", "admin console (SPA)\nS3 origin · WAF on by default")
    d.node("cog", "cognito", 500, 265, "Amazon Cognito", "user pool · MFA (TOTP)\nadmins · space-admins · operators")
    d.node("ses", "ses", 660, 265, "Amazon SES", "invite and\nverification email", size=48)
    d.node("budget", "budgets", 800, 265, "AWS Budgets", "monthly cost budget\nalert-only", size=48)
    d.node("alarms", "cloudwatch", 940, 265, "CloudWatch alarms", "per-database alarms\n+ guardrail thresholds", size=48)
    d.node("cfn", "cloudformation", 1090, 265, "AWS CloudFormation", "root stack · optional controls are\nparameters · named on Security", size=48)

    d.node("apigw", "apigw", 340, 515, "Amazon API Gateway", "HTTP API · /api/*\nJWT authorizer")
    d.group(590, 400, 470, 250, "vpc", "Agent VPC")
    d.group(620, 440, 415, 190, "private", "Private subnets")
    d.node("alb", "alb", 690, 515, "Internal ALB", "VPC Link target")
    d.node("ecs", "ecs", 830, 515, "ECS Fargate", "FastAPI REST API\nnetwork configurator")
    d.node("store", "cloudwatch", 975, 515, "Metrics store", "DynamoDB + CloudWatch\ncollection status")

    d.node("ddb", "dynamodb", 340, 765, "Amazon DynamoDB", "spaces · instances · network\nconfig · audit · rate-limits")
    d.node("s3", "s3", 500, 765, "Amazon S3", "knowledge documents\naccount · admin tier · space")
    d.node("bedrock", "bedrock", 660, 765, "Amazon Bedrock", "model catalog\nallow-lists per layer", size=48)
    d.node("sched", "eventbridge", 830, 765, "EventBridge Scheduler", "one Tier 1 + one Tier 2\nschedule per database")
    d.node("collect", "lambda", 975, 765, "Lambda · Batch", "Tier 1 collector\nTier 2 deep scan", size=48)
    d.node("plink", "privatelink", 1130, 600, "VPC peering / PrivateLink", "routes + security-group rules;\nNLB + endpoint service on overlap", size=40)

    d.group(1240, 130, 490, 760, "account", "Database account(s)", "connected from the Agent Spaces screen")
    d.node("role", "iam", 1300, 265, "IAM role", "created by you · trusts the\nagent with the space's ExternalId", size=48)
    d.node("rdsapi", "rds", 1440, 265, "Amazon RDS", "discovery: instances,\nclusters, endpoints", size=48)
    d.node("sec", "secrets", 1580, 265, "Secrets Manager", "read-only DB user\ncredentials", size=48)
    d.group(1265, 400, 440, 280, "vpc", "Database VPC")
    d.node("db", "postgres", 1400, 530, "Aurora PostgreSQL", "writer + readers", size=52)
    d.node("rdsdb", "rds", 1570, 530, "RDS PostgreSQL", "instances", size=52)
    d.node("dbvpc", "vpc", 1330, 765, "Amazon VPC", "peering accepter, routes,\nDNS resolution", size=48)

    # ── flows ──
    d.arrow("adm:t", "cf:l", via=[(90, 265)], step=1, step_at=0.35)
    d.arrow("cf:r", "cog:l", dashed=True, label="sign in · MFA", label_dy=14)
    d.arrow("adm:b", "apigw:l", via=[(90, 515)], step=2, step_at=0.75)
    d.arrow("apigw:r", "alb:l", label="VPC Link", label_dy=-8)
    d.arrow("alb:r", "ecs:l")
    d.arrow("ecs:r", "store:l", label="collection status", label_dy=-8)
    # Settings › Users / Email / Security reach these services (upwards, between rows)
    d.arrow("ecs:t", "cog:b", via=[(830, 380), (500, 380)], dashed=True, step=6, step_at=0.15)
    d.text(508, 394, "invites · groups · roles · sign-out on role change", size=10, color=INK_2)
    d.arrow((660, 380), "ses:b", dashed=True)
    d.arrow((800, 380), "budget:b", dashed=True)
    d.arrow((830, 380), "alarms:b", via=[(940, 380)], dashed=True, label="budget · alarm switches and thresholds", label_at=0.5, label_dy=14)
    # Registries, governance, schedules (downwards)
    d.arrow("ecs:b", "ddb:t", via=[(830, 690), (340, 690)], step=3, step_at=0.7, label="registries · governance · audit trail", label_at=0.4, label_dy=-6)
    d.arrow((500, 690), "s3:t")
    d.arrow((660, 690), "bedrock:t")
    d.arrow((830, 690), "sched:t", step=5, step_at=0.5)
    d.arrow("sched:r", "collect:l", dashed=True, label="runs", label_dy=-8)
    d.arrow("collect:t", "store:b", dashed=True)
    # Cross-account: connect, discover, validate secret, configure network, verify
    d.arrow((860, 483), "role:l", via=[(860, 420), (1225, 420), (1225, 265)], step=4, step_at=0.72,
            label="assume the space's role", label_at=0.35, label_dy=-6)
    d.arrow("role:r", "rdsapi:l", label="discover", label_dy=-8)
    d.arrow("rdsapi:r", "sec:l", label="validate host", label_dy=-8)
    d.arrow("role:b", "dbvpc:t", via=[(1300, 362), (1250, 362), (1250, 700), (1330, 700)])
    d.arrow((1060, 600), (1265, 600), both=True)
    d.text(1068, 558, "configure the connection,", size=10, color=INK_2)
    d.text(1068, 572, "then verify with a real login", size=10, color=INK_2)

    d.steps_panel(24, 960, 1720, [
        (1, "The administrator signs in with Amazon Cognito (TOTP) and loads the admin console from CloudFront. Super admins see everything; space admins see their spaces, their capability tier and Monitoring."),
        (2, "Every action is a REST call through API Gateway (JWT authorizer) and the VPC Link to the FastAPI service on ECS Fargate; privileged changes are written to the audit trail."),
        (3, "Agent Spaces, connected accounts, database records, network connections, governance (models, knowledge, invites) and the audit trail live in DynamoDB; knowledge documents in S3; the model catalog is read from Bedrock."),
        (4, "Connecting an account: you create a read-only IAM role that trusts the agent with the space's ExternalId; the console assumes it to discover RDS and Aurora databases, validate the read-only secret against the database's own endpoint, and set up VPC peering or PrivateLink, then verifies with a real connection."),
        (5, "Monitoring: one Tier 1 and one Tier 2 schedule per physical database in EventBridge Scheduler (cadence, pause/resume, shared by every space that includes the database); Collection shows each database's newest rows in the metrics store."),
        (6, "Settings (super admins): Users and Operators manage the Cognito pool with invites over Amazon SES; Email configures the sender; Alarms switches the per-database email alarms on or off, all or each, and sends a test email; Security reads the live posture, the cost budget and the guardrail alarms, and names this deployment's CloudFormation stack, where the optional controls are parameters."),
    ], cols=2)
    d.note(1290, 1185, ["Diagram uses the AWS Architecture Icons (aws.amazon.com/architecture/icons)."], size=9.5)
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--icons", required=True, help="path to the extracted AWS Architecture Icons package")
    ap.add_argument("--out", default=str(HERE), help="output folder (default: this folder)")
    args = ap.parse_args()
    icons = Icons(Path(args.icons).expanduser())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rsvg = shutil.which("rsvg-convert")
    for name, build in (("overview", overview), ("operator-app-architecture", operator_app), ("admin-console-architecture", admin_console)):
        svg = build(icons).render()
        svg_path = out / f"{name}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        print(f"wrote {svg_path}")
        if rsvg:
            png_path = out / f"{name}.png"
            subprocess.run([rsvg, "--zoom", "1.5", "-b", "white", "-o", str(png_path), str(svg_path)], check=True)  # nosemgrep: dangerous-subprocess-use-tainted-env-args - fixed argv, shell=False, which-resolved binary; local docs build tool
            print(f"wrote {png_path}")
        else:
            print("  (rsvg-convert not found; PNG skipped)", file=sys.stderr)


if __name__ == "__main__":
    main()
