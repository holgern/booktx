# Architecture

## Data flow

```text
source document
  -> booktx extract
  -> .booktx/chunks/*.json
  -> selected profile context/store
  -> translations/<profile>/translation-store.json
  -> booktx validate --profile <profile>
  -> translations/<profile>/translated/*.json   (compatibility export)
  -> booktx build --profile <profile>
  -> translations/<profile>/output/book.<target>.<ext>
```

## Profile isolation

`booktx` separates two concerns:

1. **Source scope**: the source file, protected names, chunk extraction, and chapter metadata.
2. **Profile scope**: translation intent, identity defaults, version history, accepted translations, durable ingest files, validation reports, and rebuilt output.

The key invariant is:

```text
Profile = hard isolation boundary
Version = history/candidate boundary inside that profile
```

Implications:

- normal `booktx translation compare` compares versions inside one selected profile;
- cross-profile inspection is explicit via `booktx profile compare`;
- `booktx build` never reads another profile's store;
- `booktx validate` only reports on the selected profile;
- one shared source can safely back multiple target languages and multiple model experiments.
