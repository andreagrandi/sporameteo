---
name: sporameteo-sentry
description: Inspect and diagnose SporaMeteo Sentry issues with the read-only Sentire CLI. Use when the user gives a Sentry issue or alert URL, asks what happened in production, or wants Sentry evidence before a code fix.
---

# SporaMeteo Sentry workflow

Sentire is a read-only CLI. It fetches issues and events and never resolves,
assigns or changes them. Use it before logs or a browser when a Sentry URL is
available.

## Credentials

SporaMeteo reports to the `andrea-grandi` Sentry organization, the same one as
Book Corners. The token that can read it is defined in
`../book-corners/.envrc`. This repository has no Sentry token of its own.

- Run every Sentire command through `direnv exec ../book-corners`, so that
  token is loaded for that one command.
- The `SENTRY_API_TOKEN` inherited by the shell, the token in
  `~/.sentryclirc` and the `sentry` CLI login cannot read this organization.
  They return 403.
- Never print a token, read `.envrc` into the output, or put a token in a URL
  or argument. Do not run `rg` or `cat` over `.envrc`.

Check access once before inspecting:

```bash
direnv exec ../book-corners sentire org list-projects andrea-grandi |
  jq 'if type == "array" then map({slug,name}) else . end'
```

If it returns 403, retry once with the Sentire config file credential:

```bash
direnv exec ../book-corners env -u SENTRY_API_TOKEN sentire org list-projects andrea-grandi |
  jq 'if type == "array" then map({slug,name}) else . end'
```

If both return 403, stop and ask the user for a token with `org:read`,
`project:read` and `event:read`. Do not read production logs and claim the
Sentry issue was inspected.

## Inspect an issue URL

Keep the URL quoted. If Sentire rejects the alert query parameters, retry with
`https://andrea-grandi.sentry.io/issues/<numeric-id>/`.

Start with the compact event fields:

```bash
direnv exec ../book-corners sentire inspect "<sentry-issue-url>" |
  jq 'if type == "object" and has("error") then . else {
    id,eventID,groupID,title,message,culprit,dateCreated,
    environment,release,tags,errors
  } end'
```

Fetch the stack frames only when the title and culprit are not enough:

```bash
direnv exec ../book-corners sentire inspect "<sentry-issue-url>" |
  jq '.entries[] | select(.type == "exception") | .data.values[].stacktrace.frames[]
    | select(.inApp) | {filename, function, lineNo}'
```

The full event can be large and can hold request data. Read only the parts the
diagnosis needs, and never paste raw payloads into GitHub.

## Frequency and timing

How often and when the error happened usually decides the diagnosis:

```bash
direnv exec ../book-corners sentire events get-issue andrea-grandi <issue-id> \
  --fields id,shortId,title,culprit,firstSeen,lastSeen,count,userCount,status,level
```

Run `sentire describe events list-issue` before listing events, to see the
fields it accepts.

Compare `firstSeen` and `lastSeen` with the last deploy, which is the end of
the `deploy` job of the latest `Checks` run on master:

```bash
gh run list --branch master --limit 3 --json name,conclusion,createdAt,updatedAt
```

## Diagnose

1. Find every project frame with `rg` and read the code around it.
2. Separate what Sentry shows from what you infer. State the exception, the
   failing code, how many times it happened, when, and whether the work it
   belonged to finished later.
3. A production database or log check needs the user's permission in this
   session. Ask for it instead of guessing.
4. Do not call a production error fixed until the change is deployed and the
   error has not come back.
