#!/usr/bin/env python3
"""Extract pinned workflow references and their comments, preserving occurrences.

Requires PyYAML, from the lint group in pyproject.toml:

    uv run --only-group lint python scripts/action-pin-comments.py REPO_ROOT

This does not replace the release rehearsal's SHA-pinning policy; it supplies
the comment verifier's input.
"""

import re
import sys
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from github_yaml import check_local, discover


def entries(node, key):
    if isinstance(node, MappingNode):
        return [value for name, value in node.value if name.value == key]
    return []


def references(document):
    for runs in entries(document, "runs"):
        if not isinstance(runs, MappingNode):
            raise ValueError("runs must be a mapping")
        for steps in entries(runs, "steps"):
            if not isinstance(steps, SequenceNode):
                raise ValueError("steps must be a sequence")
            for step in steps.value:
                yield from entries(step, "uses")
    for jobs in entries(document, "jobs"):
        if not isinstance(jobs, MappingNode):
            raise ValueError("jobs must be a mapping")
        for _, job in jobs.value:
            yield from entries(job, "uses")
            for steps in entries(job, "steps"):
                if not isinstance(steps, SequenceNode):
                    raise ValueError("steps must be a sequence")
                for step in steps.value:
                    yield from entries(step, "uses")


# Actions whose `version` input is the release of the action itself. TruffleHog's
# is the scanner image tag, and a Dependabot bump moves the pin without it (#146).
VERSION_INPUTS = {"trufflesecurity/trufflehog"}


def version_inputs(document):
    """Yield (uses node, version node) for each workflow step with a `version` input."""
    for jobs in entries(document, "jobs"):
        for _, job in jobs.value:
            for steps in entries(job, "steps"):
                for step in steps.value:
                    for uses in entries(step, "uses"):
                        for inputs in entries(step, "with"):
                            for version in entries(inputs, "version"):
                                yield uses, version


def check_version(action, claim, version):
    expected = claim.removeprefix("v")
    if not isinstance(version, ScalarNode) or version.value != expected:
        found = version.value if isinstance(version, ScalarNode) else "a non-scalar"
        raise ValueError(f"{action} passes version {found!r}, but its pin comment {claim} needs {expected!r}")


def extract(root):
    files = discover(root / ".github/workflows", root / ".github/actions")
    rows = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            # PyYAML omits comments, but token marks identify where YAML ends on
            # each line, including quoted '#' characters and flow mappings.
            ends = {}
            for token in yaml.scan(source):
                if token.start_mark.index == token.end_mark.index:
                    continue  # Synthetic block/stream ends can follow comments.
                mark = token.end_mark
                ends[mark.line] = max(ends.get(mark.line, 0), mark.column)
            document = yaml.compose(source, Loader=yaml.SafeLoader)
            claims = {}
            for node in references(document):
                if not isinstance(node, ScalarNode):
                    raise ValueError("uses must be a scalar")
                # Local and Docker references have no GitHub tag comment to check.
                if node.value.startswith("./"):
                    check_local(node.value)
                    continue
                if node.value.startswith("docker://"):
                    continue
                match = re.fullmatch(r"([\w.-]+/[\w./-]+)@([0-9a-fA-F]{40})", node.value)
                if not match:
                    raise ValueError(f"cannot check non-SHA reference: {node.value}")
                action, sha = match.groups()
                line = node.end_mark.line
                suffix = lines[line][ends[line]:]
                comment = re.fullmatch(r"\s*#\s*(\S+)\s*", suffix)
                if not comment:
                    raise ValueError(f"missing version comment or ambiguous comment for {action}")
                location = f"{path.relative_to(root).as_posix()}:{node.start_mark.line + 1}"
                rows.append("\t".join((action, sha.lower(), comment[1], location)))
                if action in VERSION_INPUTS:
                    claims[id(node)] = (action, comment[1])
            for uses, version in version_inputs(document):
                if id(uses) in claims:
                    check_version(*claims[id(uses)], version)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, IndexError) as error:
            raise ValueError(f"cannot read workflow {path}: {error}") from error
    if not rows:
        raise ValueError("no pinned actions found")
    # Emit only after every input has been read successfully.
    print("\n".join(rows))


if __name__ == "__main__":
    sys.stdout.reconfigure(newline="\n")
    try:
        extract(Path(sys.argv[1]))
    except (OSError, ValueError) as error:
        sys.exit(f"verify-action-pins: {error}")
