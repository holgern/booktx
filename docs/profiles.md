# Profiles

`booktx` translation profiles let one source book support multiple isolated
translation efforts safely.

Examples:

- `de_gpt5_5`
- `de_glm_5_2`
- `fr_gpt5_5`

## Why profiles exist

Without profiles, all mutable translation state lands in one shared store. That
mixes different languages, different model experiments, and different context
decisions.

Profiles prevent that by moving mutable translation state under
`translations/<profile>/`.

## Commands

```bash
booktx profile create ./book de_gpt5_5 --target de --target-locale de-DE --select
booktx profile list ./book
booktx profile show ./book de_gpt5_5
booktx profile select ./book de_gpt5_5
booktx profile compare ./book --profiles de_gpt5_5,de_glm_5_2 --record 0001-000001
booktx profile migrate-current ./book de_gpt5_5 --select
```

## Resolution rules

1. Explicit `--profile` wins.
2. Otherwise the active profile from `.booktx/profile-state.json` is used.
3. Otherwise exactly one existing profile is auto-resolved.
4. Otherwise target-dependent commands fail until a profile is chosen explicitly.
