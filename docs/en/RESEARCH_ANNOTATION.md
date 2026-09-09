# Research annotation guidelines: Bengali book full-page transcription

Status: **draft, pilot phase.** Companion to
[`RESEARCH_PROTOCOL.md`](RESEARCH_PROTOCOL.md) §6 and the roadmap
[`RESEARCH_ROADMAP.md`](RESEARCH_ROADMAP.md) §5. The annotation software is
`pdf_craft_tool/research/{census,annotation,annotation_server}.py` (packet S2).
`guideline_version` recorded on every gold record is **`annot-1`**.

These guidelines produce a **diplomatic transcription** — what is on the page,
not a corrected or modernised reading. Two annotators work independently and
blind; a third adjudicates. No machine draft (Tesseract, Qwen, any VLM) is shown
to an annotator, ever.

## 1. Roles and flow

1. **Census (both annotators, independent).** Before transcribing, enumerate
   every readable text region/line on the page image, top-to-bottom then the
   page's natural reading order, *including* running heads, folios, footnotes,
   marginalia, captions and catchwords. This census is independent of any OCR
   output, so text the OCR dropped is still captured.
2. **Transcription (both annotators, independent, blind).** Transcribe each
   census line from the image only.
3. **Conflict detection (software).** Lines where the two transcriptions differ
   (after NFC) become adjudication items.
4. **Adjudication (third person).** Resolve each conflict against the image;
   record the reason. The adjudicator may mark a line `unreadable`.
5. **Export (software).** Only a page whose every line is `adjudicated` and whose
   status is `final` with ≥2 annotators and a named adjudicator is exported as
   gold. `provisional` and `flagged` pages never export.

## 2. Blindness (non-negotiable)

An annotator's payload contains: the page image (or a hash-verified crop), the
census structure they themselves created, the page geometry. It does **not**
contain: OCR text, model output, candidate corrections, model identity, the
other annotator's text, or agreement statistics. Blindness is enforced in the
server payload and assignment state, not by hiding a button. If the frozen
source image hash does not match the record, annotation of that page is blocked.

## 3. Script and orthography

- **Unicode:** type in Unicode Bengali; the store normalises to NFC. Preserve
  the letters actually printed.
- **Conjuncts (যুক্তাক্ষর):** transcribe the conjunct as printed. Do not
  decompose or "spell out" (য্+ধ → দ্ধ as printed). Use ZWNJ (U+200C) / ZWJ
  (U+200D) only when the print clearly shows the non-standard form (e.g. a
  visible halant where a conjunct would be expected).
- **Vowel signs (কার) and hosonto (্):** transcribe exactly. A word printed with
  a terminal hosonto keeps it.
- **Nukta:** ড় ঢ় য় as printed; do not "normalise" ব়/ৰ or vice versa.
- **Bengali digits (০–৯):** keep as printed. Never convert to ASCII digits.
  Page numbers in Bengali digits are transcribed in Bengali digits.
- **Historical / non-standard spelling:** preserve. করিয়াছিলেন stays
  করিয়াছিলেন; ঈশ্বর vs ইশ্বর as printed. These are the point of the study.
- **Punctuation:** দাঁড়ি (।), commas, quotation marks, dashes — as printed.
  Distinguish — (em dash), – (en dash) and - (hyphen) by length where legible;
  if not distinguishable, use hyphen-minus and flag.
- **Diacritics on non-Bengali text:** transcribe Latin/Sanskrit-transliteration
  diacritics as printed.

## 4. Layout conventions

- **One census line = one printed line.** Do not merge across a line break even
  mid-word.
- **Hyphenation at line end:** keep the visible hyphen on the first line; do not
  join. A separate normalised layer (not gold) may de-hyphenate.
- **Verse:** one census line per printed verse line. Preserve indentation only
  as a note, not as spaces.
- **Reading order:** record the intended order of the census lines
  (`reading_order`). For multi-column pages, finish a column before the next.
- **Footnotes / marginalia:** separate census lines, ordered after the main text
  block they belong to, with `kind` noted.
- **Running head / folio / signature / catchword:** census lines with the
  matching `kind`; still transcribed.
- **Tables:** transcribe cell text left-to-right, top-to-bottom, one census line
  per row; note that it is a table.
- **Figures / images:** a census region with `kind="figure"` and empty text;
  transcribe only a printed caption.

## 5. Unreadable and uncertain text

- If a whole line is illegible: census line with `unreadable=true`, text empty.
  **Never guess. Never let a model fill it.**
- If part of a line is illegible: transcribe what is legible, mark the span with
  `⟦…⟧` and add a note. Count these in the exclusion report by category.
- Damaged/torn/bleed-through: transcribe the legible remainder, flag the line.
- Ambiguous single characters (e.g. ব vs র in a worn font): transcribe the more
  likely reading and flag; the adjudicator decides.

## 6. What counts as a disagreement

After NFC, two transcriptions of a line disagree if the strings are not equal.
Whitespace inside a line is collapsed to single spaces before comparison; a
leading/trailing space is ignored. A difference only in `⟦…⟧` uncertainty
marking is still a disagreement and is adjudicated.

## 7. Quality triggers (pilot)

Computed after the pilot's 60 pages, per roadmap §5:

- mean pairwise NFC **character** disagreement > **1%**, or
- exact-line agreement < **95%**, or
- any systematic policy ambiguity an annotator raises.

Any trigger ⇒ revise these guidelines, re-calibrate both annotators, and redo a
**blind 10-page recheck** before scaling to main-study annotation. The software
(`annotation.py`) blocks promotion to main-study annotation while a trigger
stands. All disagreements are adjudicated regardless of whether a trigger fired.
Revised numeric targets are frozen before main-study annotation begins.

## 8. Audit

An independent reviewer spot-checks a random sample that includes **agreements**
as well as disagreements (both annotators can share an error), inspects each
against the source image, and records findings. Report actual measured agreement
and the audit result — a passed threshold is not proof of correctness.

## 9. Provenance recorded per page

`page_id`, source SHA-256, image SHA-256, page-number convention, line
geometry (unit / render scale / orientation), reading order, each annotator id,
every revision, adjudicator id, status, `guideline_version`, measured minutes
per annotator, exclusion counts by category. Legacy Sarat gold entry ids, when a
page overlaps them, are kept as provenance links only — those selections are
re-annotated here, not trusted as gold.

## 10. Existing Sarat labels

The 216 review rows in `book-pipeline-sarat-10/gold/gold.sqlite3` (182 manual,
27 tesseract, 4 flag, 3 skip) are **candidates** for this process. They are
line-level, crop-first and single-pass; they do not establish independent
double annotation. If Sarat enters the pilot, its pages go through the full
census + blind dual transcription + adjudication like any other.
