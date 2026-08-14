# sampleproof

`sampleproof` is a deterministic, local quality-control CLI for PCM WAV sample packs. It
validates a pack against a producer-written TOML brief, measures every supported WAV without
loading its PCM payload into memory at once, groups canonical PCM duplicates, and emits both
human-readable and machine-readable evidence.

It is a Phase Goods production utility and a narrow companion to `loudnessproof`. The two tools
do not call or depend on one another: `sampleproof` handles sample-pack container, PCM, signal,
and integrity facts; `loudnessproof` remains the separate loudness-focused check.

Version 0.1 is intentionally conservative. A passing result means only that every discovered
file was analyzable and met the policy declared in that brief. Every report retains the delivery
state:

> **RENDERED — QC INCOMPLETE**

The tool does not claim that the pack has completed listening review, loudness review, loop and
crossfade review, naming or metadata review, rights clearance, malware scanning, or artistic
approval.

## Requirements and installation

- Python 3.11 or newer
- No runtime dependencies outside the Python standard library
- macOS or Linux for scanning and packet publication in version 0.1

The package can be installed and its version/configuration surfaces can be inspected on Windows,
but version 0.1 deliberately returns an operational error before scanning there. Python 3.11
cannot provide the descriptor-relative, reparse-point-safe traversal needed to make the same
source-boundary guarantee on Windows, so the tool fails closed instead of weakening it.

For an isolated local install with `uv`:

```console
uv tool install .
sampleproof --version
```

For development:

```console
uv sync --locked
uv run sampleproof --version
```

## Supported WAV scope

Version 0.1 accepts only:

- little-endian classic `RIFF` / `WAVE` files;
- classic integer PCM with format tag `1`;
- mono or stereo audio;
- 8-, 16-, 24-, or 32-bit PCM;
- a non-empty `data` chunk containing whole frames;
- exactly one `fmt ` chunk and exactly one `data` chunk;
- bounded RIFF chunks, including the required pad byte after odd-sized chunk payloads; and
- unknown ancillary chunks when their declared bounds are valid.

Version 0.1 explicitly rejects `WAVE_FORMAT_EXTENSIBLE` (`0xFFFE`). It also rejects floating
point WAV, compressed WAV, `RIFX`, RF64, BW64, more than two channels, malformed RIFF sizes,
duplicate required chunks, inconsistent block alignment or byte rate, and partial PCM frames.
Those are unsupported inputs, not policy failures, so they make the run **incomplete**.

The scanner recursively finds regular files with a case-insensitive `.wav` suffix. On macOS and
Linux it opens the source root and every path component relative to fixed directory descriptors,
with no-follow semantics. A symbolic-link source root, file, intermediate directory, or raced
replacement therefore cannot redirect measurement outside the declared tree. Results are sorted
by normalized relative POSIX path, then by the original path, so report order is stable.

## What is measured

All PCM measurements are streamed in bounded blocks:

Before parsing, the bytes behind each securely opened source handle are streamed to a private
temporary scratch file while the whole-file digest is calculated. Parsing and measurement use
that fixed scratch snapshot; the source handle is then rehashed and its identity and visible
pack-relative path are revalidated. The scratch file is closed and removed before the command
returns. This uses bounded memory but requires temporary disk space proportional to the largest
WAV being analyzed.

- **Sample peak:** the largest absolute decoded integer sample, normalized by
  `2^(bit_depth-1)`. This is sample peak, not true peak or inter-sample peak. Silence has a
  numeric peak of `0.0` and a JSON `sample_peak_dbfs` value of `null`.
- **Full-scale samples and frames:** samples equal to either integer endpoint, plus frames that
  contain at least one endpoint sample. Endpoint counting is evidence; it is not a clipping
  determination.
- **DC offset:** the arithmetic mean of normalized raw samples, independently for each channel.
  No filter or perceptual weighting is applied.
- **Digital-zero boundaries:** all-zero status, leading and trailing all-zero frame counts, and
  first and last non-zero frame indexes. A stereo frame is zero only if both samples are zero.
- **File SHA-256:** a digest of every source-file byte, including container and metadata bytes.
- **Canonical PCM SHA-256:** a domain-separated digest over channel count, sample rate, bit
  depth, frame count, and exact PCM bytes. It intentionally ignores ancillary RIFF chunks so
  files with the same supported audio and format group together even when container metadata
  differs.

SHA-256 is used for deterministic integrity and duplicate evidence. It is not a rights,
authenticity, provenance, or security determination.

## Brief format

The configuration is strict TOML. Unknown keys, missing required fields, booleans where integers
are required, duplicate allowlist entries, unsupported bit depths or channel counts, non-finite
numbers, and values outside the documented ranges are rejected.

```toml
schema_version = 1

[delivery]
pack_id = "form-under-load-01"
title = "FORM UNDER LOAD 01"
version = "1.0.0"
license = "Commercial sample license"

[pcm]
allowed_sample_rates = [44100, 48000]
allowed_bit_depths = [24]
allowed_channels = [1, 2]

[policy]
max_sample_peak_dbfs = -0.1
max_full_scale_samples = 0
max_abs_dc_offset = 0.001
all_zero = "fail"
duplicate_pcm = "warn"
```

`delivery.pack_id`, `delivery.title`, `delivery.version`, and `delivery.license` must be non-empty
strings. All three PCM allowlists are required and non-empty. Sample rates must be integer values
from 1 through 768000. Allowed bit depths are `8`, `16`, `24`, and `32`; allowed channel counts
are `1` and `2`.

Signal thresholds are optional. Omitting one disables that threshold rather than silently
inventing a universal limit:

| Policy key | Meaning | Omitted behavior |
|---|---|---|
| `max_sample_peak_dbfs` | Maximum sample peak in dBFS; must be finite and at most `0.0` | Not assessed |
| `max_full_scale_samples` | Maximum integer-endpoint sample count | Not assessed |
| `max_abs_dc_offset` | Maximum absolute per-channel raw DC mean, from `0.0` to `1.0` | Not assessed |
| `all_zero` | `allow`, `warn`, or `fail` | `fail` |
| `duplicate_pcm` | `allow`, `warn`, or `fail` | `fail` |

The values in [the example brief](examples/sampleproof-example.toml) are an example declaration
for one project, not recommended mastering or sample-pack standards.

## Commands

Analyze without writing a packet. Markdown is written to standard output:

```console
sampleproof check BRIEF.toml SOURCE_ROOT
```

Print the same result as stable, indented JSON:

```console
sampleproof check BRIEF.toml SOURCE_ROOT --json
```

Build a new three-file packet and also print the selected Markdown or JSON report:

```console
sampleproof build BRIEF.toml SOURCE_ROOT --output NEW_OUTPUT_DIRECTORY
sampleproof build BRIEF.toml SOURCE_ROOT --output NEW_OUTPUT_DIRECTORY --json
```

`build` requires the output parent to exist, requires the output to be outside the scanned source
tree, and refuses any existing file, directory, or symbolic link at the target path. It writes
the complete packet in a same-parent staging directory, flushes every file, checks the target a
second time, and publishes with a same-filesystem directory rename. Its own staging directory is
removed on failure.

On macOS and Linux, staging creation, report writes, cleanup, and publication are relative to one
fixed output-parent descriptor. The final publish uses the operating system's atomic no-replace
primitive. If either that primitive or the required safe traversal is unavailable, `build`
returns an operational error instead of weakening the boundary or non-overwrite guarantee.

The packet contains exactly:

- `sampleproof-report.json`: schema-versioned measurements, declarations, findings, duplicate
  groups, hashes, and method definitions;
- `sampleproof-report.md`: a concise review document with the incomplete-QC state and method
  boundaries kept visible; and
- `sampleproof-manifest.jsonl`: one unambiguous JSON object per discovered WAV, in path order,
  containing `path`, `sha256`, and `size_bytes`.

No source audio is renamed, modified, normalized, repaired, or deleted, and no audio asset is
published in the packet. The private measurement snapshot described above is temporary and is
removed before the command returns.

## Outcomes and exit codes

| Exit code | Outcome | Meaning |
|---:|---|---|
| `0` | `pass` | Every discovered WAV was analyzed and no declared fail-level rule was violated. Warnings may exist. |
| `2` | `fail` | Analysis completed, but at least one declared fail-level policy rule was violated. |
| `1` | `incomplete` / operational error | An input was unsupported or malformed, no WAV was found, configuration was invalid, discovery failed, or a packet could not be written safely. |
| `130` | interrupted | The process received a keyboard interrupt while running. |

A malformed file does not stop the remaining discovered WAVs from being measured. Its stable
parser error code and message appear in the file record, and the overall outcome remains
`incomplete` even if other files pass policy.

## Report-schema notes

The JSON root includes `schema_version`, `tool`, `delivery_state`, `brief`, `source_root`,
`result`, `files`, `duplicate_groups`, `findings`, and `definitions`. Version 0.1 emits only finite
JSON numbers; silence uses `null` for logarithmic peak. Every finding contains a stable `code`,
`severity`, affected `paths`, a human message, and machine-readable `observed` and `expected`
values where applicable. Portable output records `source_root` as `.`; it never exposes the
machine's absolute source path.

The canonical PCM fingerprint is defined as SHA-256 over this byte sequence:

1. ASCII `sampleproof-pcm-v1` followed by a zero byte;
2. little-endian unsigned channel count (`uint16`);
3. little-endian unsigned sample rate (`uint32`);
4. little-endian unsigned bit depth (`uint16`);
5. little-endian unsigned frame count (`uint64`); and
6. the exact bytes of the single validated PCM `data` chunk.

## Development and verification

```console
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build --no-build-isolation
```

The build command above uses the exact Hatchling version in the locked development environment.
CI runs the full locked suite on Ubuntu for Python 3.11–3.14 and on macOS for the 3.11/3.14
endpoints. Windows 3.11/3.14 jobs verify portable configuration/version behavior and the explicit
fail-closed scanning contract. CI also installs and smokes the built wheel and source
distribution. Third-party GitHub Actions are pinned to full commit SHAs.

## License

MIT. See [LICENSE](LICENSE).
