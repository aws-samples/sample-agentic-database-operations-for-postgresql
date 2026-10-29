# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment

For anything that happened during a deploy, the AWS region, the exact `make` command you ran, and the stack status from
`make outputs` will usually pin the cause straight away.

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Running the checks locally

Two commands cover step 3 above, and neither needs AWS credentials or a deployment:

```bash
make check    # template linting, resource naming, deploy settings, container runtime
make test     # unit tests plus the guard tests for the deploy tooling
```

`make check` parses every CloudFormation template and verifies that resource names stay within
their AWS length limits, that every parameter the Makefile passes actually exists in the template,
and that `deploy.config.example` has not drifted from the template defaults.

`make test` includes guards that exist because the thing they watch has broken before: that the
destroy confirmation cannot be walked past, that no destructive AWS call appears outside
`deployment/destroy/`, and that the read-only checker stays read-only. If you add anything that can
delete a resource, it belongs in `deployment/destroy/` and it must carry its own confirmation. The
tests will tell you if it does not.

If you change the backend, `python3 -m compileall backend` and a `pyflakes` pass should both be
clean. If you change either web app, `npm run build` should succeed in that directory.

## Deploying your change

`make deploy` is safe to run repeatedly and will update an existing stack rather than recreate it.
Expect 25 to 40 minutes for a first deploy. `make destroy` asks you to type `<stack>@<account>`
before it removes anything, because the account is the part that is easy to get wrong.

Please do not commit `deploy.config`. It holds your email address and an account-specific bucket
name, and it is gitignored for that reason.

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
