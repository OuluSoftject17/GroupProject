# Contributing
Contributions from outside sources are not accepted. Unless part of the group,
do not open pull requests or issues.

In order to remain within the expectations set by the course, we need to follow
a few guidelines. This document serves to outline those guidelines and serve as
a reference in how to proceed.

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL
      NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED",  "MAY", and
      "OPTIONAL" in this document are to be interpreted as described in
      [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119).

## Branching & commit conventions
Fork the project and perform development on a separate branch to main. A pull
request SHOULD be opened early and the WIP/Draft label of GitHub is RECOMMENDED
to be made use of, as this will help us avoid making duplicate work. Every
commit MUST start with a `<keyword>:` that describes which part of the project
the commit focuses on. For instance a commit that affects the simulator setup
could look like `simulator: further restricted memory`, and a commit focused on
improving the testing of the legacy component could be `legacy-test: ensure
values are within bounds`.

## Code
All code committed to the repository MUST be (co-)authored by an LLM. Similarly,
all code committed to the repository MUST be reviewed. Every pull request SHOULD
be reviewed by someone other than the author so that no ambiguity remains about
whether the code was properly reviewed or not. Similarly, all code contributions
MUST include the prompts used to generate the code, stored in a markdown file
titled `<PR-number>.md` under the `documentation/prompts` folder.

It should be noted that the CI/CD infrastructure is likened to code, and has the
same restrictions as above.

## Documentation
Any other general knowledge about the system MUST be stored in the
`documentation` folder, with the exception of meeting notes which are stored
in the path `documentation/meetings`.
