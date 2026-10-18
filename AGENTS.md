# SporaMeteo agent instructions

## Scope

- `SporaMeteo_PRD_Epic_1_Validation_EN.md` defines what Epic 1 covers.
  `SporaMeteo_Context_Dossier_EN.md` is background only.
- Epic 1 experiments check what is possible. They do not decide what goes into
  the first version of the app.
- Never buy a domain, server, paid API or subscription.

## Plain language

These rules apply to everything a person reads: issues, reports, docs, code
comments, commit messages, pull requests and replies.

Prefer the explicit phrase over the technical term. Say what happens instead
of naming the technique.

| Avoid | Write |
|---|---|
| nowcast | conditions today, based on the weather of the last 7 to 30 days |
| leakage, point-in-time cutoff | only data that existed on that date |
| reanalysis | reconstructed past weather |
| archived forecast runs | forecasts published in the past |
| lead time | days ahead |
| pseudo-absences, background points | random places where nobody reported a find |
| holdout | years and areas kept aside for testing |
| bootstrap confidence interval | uncertainty range |
| ablation | compare weather alone, habitat alone, and both |
| baseline | simple guess, such as a random ranking |
| effective resolution | real level of detail |
| spike | prototype |
| smoke test | check that the API answers |
| payload | data size per request |

- When a technical name is needed to find docs or a library, such as ERA5,
  logistic regression or LightGBM, explain it in plain words the first time.
- Use plain names for columns, files and functions, such as `assessment_date`
  and `days_ahead`.
- One idea per sentence, active voice, whole sentences with articles and verbs.
  No arrows or symbol shorthand.
- Use plain words: "use", not "leverage" or "utilize". Avoid crucial, delve,
  enhance, pivotal, robust, seamless, showcase and underscore.
- No em dashes, no parentheses, no curly quotes. End the sentence or use a
  comma. Use colons only before a list or an example.
- No filler, hedging, chatbot phrases, emojis, or bold on every noun. Use
  sentence case headings.
- Name the mechanism, the file or the number. Cut any sentence that could sit
  unchanged in another project's docs.
- Before delivering text, reread it and fix anything that reads as machine
  generated or needs a specialist to decode.

## Issues

Use the `gh-issue` skill in `.agents/skills/gh-issue/` to create or change
GitHub issues.

## Sentry

Use the `sporameteo-sentry` skill in `.agents/skills/sporameteo-sentry/` to
read a Sentry issue or alert.
