# Quickstart

## 1. Initialize a source project

```bash
booktx init ./demo --source-file book.epub --source-lang en
```

## 2. Extract the source

```bash
booktx extract ./demo
```

## 3. Create and select a translation profile

```bash
booktx profile create ./demo de_gpt5_5 \
  --target de \
  --target-locale de-DE \
  --model codex-openai/gpt-5.5@low \
  --select
```

## 4. Initialize the profile-local context

```bash
booktx context init ./demo --profile de_gpt5_5 --non-interactive
booktx context questions ./demo --profile de_gpt5_5
# Ask the user to approve or edit answers before continuing.
booktx context approve ./demo --profile de_gpt5_5 Q001 --text "<USER_APPROVED_TEXT>" --approved-by "user:<USER>"
booktx context render ./demo --profile de_gpt5_5 --write
booktx context mark-ready ./demo --profile de_gpt5_5
```

## 5. Request a translation task

```bash
booktx translate next ./demo --profile de_gpt5_5 --unit batch --max-words 800 --format block
```

Read `translations/de_gpt5_5/context.md`, then fill the generated durable file
under `translations/de_gpt5_5/ingest/`.

## 6. Submit the translation

```bash
booktx translate insert ./demo \
  --profile de_gpt5_5 \
  --task-id TASK \
  --file translations/de_gpt5_5/ingest/TASK.block.txt \
  --format block
```

## 7. Validate and build

```bash
booktx validate ./demo --profile de_gpt5_5
booktx build ./demo --profile de_gpt5_5
```

The rebuilt output is written under:

```text
demo/translations/de_gpt5_5/output/
```

## Legacy projects

Old single-layout projects can be migrated with:

```bash
booktx profile migrate-current ./demo de_gpt5_5 --select
```

## Context approval

booktx never decides translation policy by itself. An agent may propose context answers, but the user must approve them before translation begins. Do not use `context mark-ready --force` during normal translation work.
