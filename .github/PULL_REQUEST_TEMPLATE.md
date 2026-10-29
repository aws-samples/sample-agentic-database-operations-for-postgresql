**What does this change, and why?**

Describe the behaviour before and after. If it fixes an issue, link it.

**How did you verify it?**

Say what you actually ran, not what should pass.

- [ ] `make check` is clean
- [ ] `make test` is clean
- [ ] Backend changes: `python3 -m compileall backend` and `pyflakes` are clean
- [ ] Web app changes: `npm run build` succeeds in the affected app
- [ ] Template changes: deployed to a real account, or explain why not

**Does it touch anything security-relevant?**

Tick anything that applies, and say what you did about it.

- [ ] The read-only path to customer databases
- [ ] Anything that can delete a resource, which belongs in `deployment/destroy/` with its own confirmation
- [ ] IAM policies, or the cross-account role
- [ ] Authentication, authorisation, or a REST handler's access gate
- [ ] Adds a dependency (note its license; `NOTICE` needs updating)

**Documentation**

- [ ] `README.md` still matches the behaviour
- [ ] `docs/ARCHITECTURE.md` still matches, if internals changed
- [ ] `CHANGELOG.md` has an entry under `Unreleased`

**Licensing**

By submitting this pull request, I confirm that my contribution is made under the terms of the
Apache-2.0 license, and that new source files carry the SPDX header used elsewhere in the repository.
