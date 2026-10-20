# Vendored attestation schemas (P3-03)

JSON Schemas (draft 2020-12) transcribed by hand from the normative specs, which publish
protobuf and prose rather than JSON Schema:

- `in-toto-statement-v1.schema.json`: in-toto Attestation Framework v1, the Statement layer and
  ResourceDescriptor (https://github.com/in-toto/attestation/tree/main/spec/v1).
- `slsa-provenance-v1.schema.json`: the SLSA Provenance v1.0 predicate
  (https://slsa.dev/spec/v1.0/provenance).

Required fields, field types and "at least one of uri, digest, content" follow the specs; unknown
fields are allowed where the specs allow extension. `builder.id` is a plain string here because
EBS uses `ebs@<version>` (task P3-03 R1). Updating is manual: re-read the spec, edit, keep this
note current.
