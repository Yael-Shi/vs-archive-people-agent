# Gemini mention pilot

This is an experiment. It is not an official pipeline step, not a checkpoint step, and not a versioned artifact.

The official command is `find-mentions`. It publishes `mention-proposals-v1` for a whole discovery selection and may also write `mention-proposal-diagnostics-v1`. This pilot stays available for comparison. It still writes `GEMINI_MENTION_PILOT_NOT_ARTIFACT`, still has no `schema_version`, and still is not a freeze input. Official proposals are not fed to v1 freeze.

`RUN_STEPS` stays `discover -> freeze-evidence -> resolve -> review`. This pilot does not emit `mention-proposals-v1`, does not freeze evidence, and does not resolve identity.

## What Gemini may do

Gemini 3.8 Flash may propose text spans that look like person names: a full name, a partial name, a title plus a name, a nickname, or an uncertain person-reference surface.

The model must copy the visible spelling. It must not correct OCR or transliteration.

A title or role with no name component is not a candidate. `הנשיא` by itself is rejected. `ד"ר משה מרזוק`, `השגריר האמריקאי ג'פרסון קאפרי`, and `הלורד מויו` stay, and the title is kept on that same mention.

One title-bearing proposal is one published mention. A Gemini or known-name span whose offsets are exactly that mention's name slice is absorbed into it. The published candidate stores three exact source slices when the model supplies them:

- `exact_mention` is the full source slice, including a title or an attached prefix when that is part of the copied span
- `name_surface` is the name-only slice
- `title_surface` is the title or role slice, or null when there is no title

`body_text[name_start_offset:name_end_offset] == name_surface`, and the same check applies to the title when it is present. Both slices sit inside the full mention. A split is stored only when each piece is a unique exact substring. Characters outside those slices may be whitespace, the original line break, or a safe attached Hebrew proclitic. Between the title slice and the name slice, Unicode punctuation and other Unicode separators, such as a comma, may also stay uncovered. Letters or digits in that gap are rejected. Nothing is rewritten.

VS Archive titles are free text on `Person.honorific`. This pilot does not keep a title vocabulary and does not infer a title from the first token. Gemini supplies the semantic split. For `kind` `TITLE_AND_NAME`, `surface`, `title_surface`, and `name_surface` are all required, and each must be an exact source slice under the check above. `והאלוף` stays `והאלוף`. A missing or invalid split is unaligned with `TITLE_SPLIT_UNRESOLVED`. The exact full surface stays on that record. It is not published with the full phrase as the name, and it is not rewritten as `FULL_NAME`. Two different exact splits of the same full span are not merged. Consolidation raises `PILOT_SPLIT_CONFLICT` and publishes neither.

An occurrence used only as part of a place, institution, street, or organization is not a person candidate. The model may return that exact occurrence with `referent` `NON_PERSON`. A person occurrence uses `PERSON` or null. `פארוק` in `רחוב פארוק`, `פואד הראשון` in `רחוב פואד הראשון`, and `סולימאן פאשה` in `רחוב סולימאן פאשה` are rejected for those occurrences. The same string is still eligible where it names a person. Local code also drops a span that continues the word `רחוב`, including after an attached proclitic of up to four letters such as `ברחוב`. That is not a list of street names. A proposal with `referent` `NON_PERSON` drops that aligned span and any known-name span inside it.

When the model selects a core name inside an attached proclitic, the name offsets are that exact remainder: `לאהרוני` can yield `אהרוני`, `לויקטור לוי` can yield `ויקטור לוי`, and `לאיסר כראל` can yield `איסר כראל`. The full slice still contains the prefix. A peel is accepted only for a one-to-four-letter prefix from `ה ו ב כ ל מ ש` that includes at least one of `ו ב כ ל מ`, and only when that remainder is the proposed name. A peel made only of `ה` or `ש` is not applied, so `שלום` does not become `לום` and `הלורד` does not become `לורד`. If the proposal omits `name_surface`, the full slice is the name and no letters are removed.

## What Gemini must not do

- assign a production `person_id`
- choose a canonical identity
- create an alias
- suggest a merge
- approve a Person
- modify production
- read Django, AWS, or a database

The provider call has no web search and no tools. The repository computes source offsets and checks:

```python
body_text[start_offset:end_offset] == exact_mention
```

A Gemini string that is not an exact slice of the alignment window is recorded under `unaligned_gemini_candidates` and gets no offset. The window is one segment, or two adjacent segments when the proposal crosses a single newline.

## Command

One displayed body per run. No archive-wide batch.

```bash
.venv/bin/python -m vs_archive_people.cli pilot-find-mentions \
  --documents data/document_text_snapshot.json \
  --archive-item-id 10 \
  --evidence-source ocr_document.displayed_transcription.text \
  --identity data/identity_snapshot.json \
  --output runs/gemini-mention-pilot.json
```

`--evidence-source` is required when the archive item has more than one displayed body. It may be one role of a merged body, for example `ocr_document.displayed_transcription.text`. The output path is refused when it already exists. The command reads `GEMINI_API_KEY` from the environment and fails if that variable is missing or blank. It does not read Secrets Manager.

The published JSON uses `"kind": "GEMINI_MENTION_PILOT_NOT_ARTIFACT"`. It has no `schema_version`. `body_sha256` is the SHA-256 of the exact UTF-8 body, with no Unicode normalization.

## Segmentation

The body is split on `\n`. Newline code points are not part of segment text. Carriage return is left as-is. Empty lines are omitted. Each remaining line is `S0001`, `S0002`, and so on, in order. Each segment also carries `break_after`, the exact characters between it and the next segment.

A proposal may set `end_segment_id` to the next segment when `break_after` is a single `\n`. The surface must then be an exact slice of the original body from the start segment through that next segment, including `\r` and `\n` when they are there. `האלוף\r\nבנימין גיבלי` and `שמואל\r\nעזר` are aligned that way. A space substituted for the break, or a missing `\r`, does not match and gets no offset. A blank line (`break_after` other than `\n`) is not joined.

A line longer than 800 code points is split. A space in the latter half of that window is the boundary when one exists; otherwise the split is the code-point limit. Characters are not rewritten. `body_text[start:end] == segment.text` for every segment.

Segments are sent to Gemini in batches of at most 20,000 code points of segment text.

## Repeated surfaces

Matches are non-overlapping and left to right inside the alignment window. One match is aligned. More than one match is aligned only when Gemini sets `occurrence` to the 1-based index of that match and the index is in range. Otherwise the proposal is unaligned with `AMBIGUOUS_REPEATED_SURFACE` or `OCCURRENCE_OUT_OF_RANGE`. The first match is not chosen silently.

## Known names

Accepted `Person.name` and accepted `PersonAlias.name` values from `identity-snapshot-v1` are searched as literal substrings. Pending persons, pending aliases, and aliases of pending persons are excluded. Honorifics are not searched. There is no fuzzy match, no casefold, no NFKC, and no title stripping. The same exclusion rules as resolution supply the name strings. The pilot result still has no `person_id`.

`comparison` is `KNOWN_NAME`, `GEMINI`, or `BOTH`. `BOTH` means both detectors found the same exact slice. Overlapping slices that are not the same span are both kept. A known-name hit is a recall signal. It does not prove that the occurrence names a person. A hit that continues `רחוב`, or that sits inside a span Gemini marked `NON_PERSON`, is not published.

Every known-name span has `known_name_match_quality`. That field is copied onto a consolidated candidate when `KNOWN_NAME` contributed, and is null when only Gemini found the slice.

A neighbor counts as part of a name token when its Unicode category is a letter (`L*`) or a number (`N*`). Combining marks (`M*`) and format characters (`Cf`) are skipped on the way to that neighbor. Punctuation, symbols, whitespace, and either end of the body are boundaries.

- `EXACT_BOUNDARY`: neither side continues a name token. `Dana` in `Dana Cohen` is this, because a space separates the spans. Both spans are kept.
- `PREFIXED`: the right side is a boundary, and the left side is one to four characters drawn only from the Hebrew proclitic letters `ה ו ב כ ל מ ש`. `ומשה` for the name `משה` is `PREFIXED`. A niqqud mark on that letter is ignored, so `בְמשה` is also `PREFIXED`.
- `EMBEDDED`: any other continuation, including a letter that is not in that closed set. The exact string `Dana` inside `ADana` is `EMBEDDED`. `Adana` does not contain `Dana`, so casefolding is not used to invent that hit. Embedded matches stay in the result.

The prefix set is not a morphological analysis. A left run longer than four of those letters, or a right-side letter, is `EMBEDDED`.

## Provider call

Dependency: `google-genai==1.63.0`, the same version locked in the main VS Archive backend. That SDK exposes `thinking_level` (`ThinkingLevel.LOW`) and `response_json_schema`. The model id is the string `gemini-3.8-flash`.

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key=api_key,
    http_options=types.HttpOptions(timeout=120_000),
)
response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=segment_json,
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_json_schema=CANDIDATE_RESPONSE_SCHEMA,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
        ),
        tools=None,
        safety_settings=None,
    ),
)
```

`HttpOptions.timeout` is milliseconds. Temperature, `top_k`, `top_p`, and `thinking_budget` are left unset so decoding stays at the model default. `ThinkingLevel.LOW` is the SDK enum whose value is `"LOW"`; the enum is case-insensitive, so this is the installed form of `thinking_level="low"`. Pydantic is not a direct dependency. The SDK accepts the plain JSON Schema, and this package checks the returned JSON with `jsonschema`.

Each call record may include the requested model, `model_version`, `response_id`, finish reason, prompt block reason, and token counts when the SDK exposes them. The API key is not stored on the detector and is not written to the output. Full document bodies are not logged. A finish reason other than `STOP` or `FINISH_REASON_UNSPECIFIED` fails the call. `MAX_TOKENS` does not become a partial candidate list.

## Structured provider schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["candidates"],
  "properties": {
    "candidates": {
      "type": "array",
      "items": {
        "anyOf": [
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["segment_id", "surface", "kind", "name_surface", "title_surface"],
            "properties": {
              "segment_id": {"type": "string"},
              "surface": {"type": "string"},
              "kind": {"enum": ["TITLE_AND_NAME"]},
              "name_surface": {"type": "string"},
              "title_surface": {"type": "string"},
              "occurrence": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
              "end_segment_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
              "referent": {"anyOf": [{"enum": ["PERSON", "NON_PERSON"]}, {"type": "null"}]}
            }
          },
          {
            "type": "object",
            "additionalProperties": false,
            "required": ["segment_id", "surface"],
            "properties": {
              "segment_id": {"type": "string"},
              "surface": {"type": "string"},
              "kind": {"anyOf": [{"enum": ["FULL_NAME", "PARTIAL_NAME", "NICKNAME", "UNCERTAIN_PERSON_NAME"]}, {"type": "null"}]},
              "name_surface": {"anyOf": [{"type": "string"}, {"type": "null"}]},
              "title_surface": {"anyOf": [{"type": "string"}, {"type": "null"}]},
              "occurrence": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
              "end_segment_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
              "referent": {"anyOf": [{"enum": ["PERSON", "NON_PERSON"]}, {"type": "null"}]}
            }
          }
        ]
      }
    }
  }
}
```

The two `anyOf` branches are mutually exclusive by `kind`. A `TITLE_AND_NAME` row must include string `name_surface` and string `title_surface`; null or omission fails the provider schema. The other branch covers the remaining kinds, including a null or omitted kind, and still allows those surfaces to be omitted or null. Empty strings remain schema-valid and are rejected locally. There is no offset field and no person id. `additionalProperties` stays `false`. On a non-title row, omitted `name_surface` means the full `surface` is the name. An empty name, or a name that is only `הנשיא`, `הנשיאה`, `הוא`, `היא`, `האיש`, or `האישה`, is not published.

The structured-output schema was accepted in live pilot runs, including corpus items 22, 156, 182, and 184.

## Provider failures

Retries belong to `google-genai`. The client sets `HttpRetryOptions(attempts=3)` and leaves status codes, initial delay, max delay, exponential base, and jitter unset, so the SDK's default transient-status list and backoff apply. Three is the total number of attempts, including the first call. The request timeout stays 120000 ms.

`people-agent` calls `generate_content` once per batch. It has no second retry loop and no sleep. After the SDK finishes its retries, `google.genai.errors.APIError` (including `ClientError` and `ServerError`) becomes `PilotProviderError` with `error_code` `PILOT_PROVIDER`. The message may include the SDK class name, the integer `code`, and a `status` that is only letters, digits, and underscores. Response details, the prompt, and the document body are not copied. `UnknownApiResponseError` becomes a fixed unreadable-response message. `httpx.RequestError`, which the SDK can raise for timeout and transport failure without wrapping, becomes `provider transport failed: <exception class>`. Other exceptions, including programmer errors, propagate. The original exception is not chained, because its text can contain the request.

## Corpus used for comparison

The first official pilot evaluation used ten archive items, in this order:

| Archive item | Evidence source |
| --- | --- |
| 22 | `ocr_document.displayed_transcription.text` |
| 156 | `ocr_document.displayed_transcription.text` |
| 182 | `ocr_document.displayed_transcription.text` |
| 184 | `ocr_document.displayed_transcription.text` |
| 268 | `ocr_document.displayed_transcription.text` |
| 273 | `ocr_document.displayed_transcription.text` |
| 288 | `manual_text.body` |
| 292 | `ocr_document.displayed_transcription.text` |
| 297 | `ocr_document.displayed_transcription.text` |
| 323 | `ocr_document.displayed_transcription.text` |

The selection file is `gemini_real_pilot_selection_2026-10-11.json`. The refined pilot result, including the displayed body used for each item, is `gemini_real_pilot_results_refined_2026-10-12.json`. An earlier result from 2026-10-11 is also stored. Those files are local evaluation artifacts. They are not versioned pipeline artifacts and they are not rewritten by `compare-mention-runs`.

The displayed bodies in the refined result match the 2026-08-29 live-text export `vs_archive_live_texts_20260829T185444Z.json` for these ten items. That export is schema version `1`. It is not itself `document-text-snapshot-v1`. The historical identity file is `vs-archive-biography-production-identity-snapshot-v1`. It is not `identity-snapshot-v1`.

`compare-mention-runs` can read the refined pilot file. It does not call Gemini. An official `mention-proposals-v1` run over this corpus was not found in the repository or in the broader filesystem search. That search also found no additional recoverable pilot corpus artifact. This does not establish that no other corpus ever existed.

The displayed bodies for these ten items were copied, without changing the text, into ignored `data/mention-corpus-2026-10-11/document_text_snapshot.json` as `document-text-snapshot-v1`. `discover` wrote ignored `runs/mention-corpus-2026-10-11/discovery.json` for exactly those ids. The historical identity file is still `vs-archive-biography-production-identity-snapshot-v1`. `project_to_identity_snapshot_v1` copies it to `identity-snapshot-v1` without dropping alias rows. Same-person alias strings that normalize together stay one person. See ADR-041.
