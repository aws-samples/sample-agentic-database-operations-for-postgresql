## Intended Use

This is sample code, for non-production usage. It should not be deployed to a
production environment without additional security testing. You should work with
your security and legal teams to meet your organization's security, regulatory and
compliance requirements before deployment.

The deployment connects to databases you nominate and reads their catalogs and
statistics. All database access is read-only and passes through a single safety
gate, but a deployment is still only as sensitive as the databases it is pointed at,
and it holds the credentials needed to reach them. Scope it accordingly.

For what the deployment protects, how it does so, and the static-analysis findings
that are accepted by design with the reasoning for each, see the
[security model](docs/ARCHITECTURE.md#security-model).

## Reporting Security Issues

We take all security reports seriously. When we receive such reports,
we will investigate and subsequently address any potential vulnerabilities as
quickly as possible. If you discover a potential security issue in this project,
please notify AWS/Amazon Security via our
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to [AWS Security](mailto:aws-security@amazon.com).
Please do *not* create a public GitHub issue in this project.
