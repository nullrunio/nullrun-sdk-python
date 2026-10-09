#!/usr/bin/env bash
# Reject a Co-Authored-By trailer in a commit message.
#
# Why this exists as a hook rather than a convention: the rule is in both
# CLAUDE.md files and was still broken twice. A convention that has already
# failed is not a convention, it is a hope. This fails the commit, so the
# next person finds out at `git commit` rather than in a blame view.
#
# Not a trailer ban in general — the CHANGELOG legitimately credits people,
# and a commit BODY mentioning "Co-Authored-By" while discussing the rule
# (this file's own history, a revert commit) is not a violation. What is
# banned is the trailer: a `Co-Authored-By:` line at the end of the message.
# pre-commit passes the message file as $1.

set -euo pipefail

msg_file="${1:?commit-msg hook needs the message file}"

if [ ! -f "$msg_file" ]; then
  echo "reject-coauthored-trailer: no message file at $msg_file" >&2
  exit 1
fi

# Strip comment lines (everything after a core.commentChar `#`), then look
# for the trailer at the start of a line. Anchored, so prose that merely
# mentions the string is not a violation.
if grep -qiE '^[[:space:]]*(co-authored-by|co-authored)[[:space:]]*:' "$msg_file"; then
  {
    echo "error: Co-Authored-By trailer rejected."
    echo
    echo "  This repository's commits are authored solely by the repo owner."
    echo "  Remove the trailer and re-commit:"
    echo
    grep -niE '^[[:space:]]*co-authored' "$msg_file" | sed 's/^/    /'
    echo
    echo "  If you are reverting or documenting a commit that HAD the trailer,"
    echo "  say so in the message body in prose — the trailer form is what is"
    echo "  rejected, not the words."
  } >&2
  exit 1
fi

exit 0
