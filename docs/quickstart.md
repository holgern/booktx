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
booktx context mark-ready ./demo --profile de_gpt5_5 --force
```

## 5. Request a translation task

```bash
booktx translate next ./demo --profile de_gpt5_5 --unit batch --max-words 500 --format block
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
