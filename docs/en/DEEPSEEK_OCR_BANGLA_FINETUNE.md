# Fine-tuning DeepSeek-OCR-2 for printed Bengali — feasibility study

Date: 2026-09-11. Status: desk research plus local probes; no fine-tuning run yet.

## Question

Can fine-tuning DeepSeek-OCR-2 on Bengali data make it the best printed-Bengali
OCR engine in our benchmark (`workflow/`, paper repo
`bengali-ocr-benchmark-paper`)?

## Verdict

- **Large gains over zero-shot are near-certain.** In every comparable
  adaptation we found, LoRA/QLoRA turned a collapsed model into a usable one.
  On Sinhala pages, DeepSeek-OCR-2 went from 96.11% to 6.94% CER.
- **Beating every other engine is unlikely, and it depends on the split.**
  - **Mozhi word crops:** the bar is bbOCR at 1.3% CER. This bar is very hard
    to reach; no published DeepSeek-OCR adaptation gets close on a
    crop-recognition task.
  - **REID2019 pages:** the bar is Surya at 15.7% CER. This one is plausible,
    but only with real page-level training data. In the Sinhala analogue,
    DeepSeek-OCR-2 did beat Surya (6.94% vs 8.84%).
- **DeepSeek-OCR-2 is probably not the best base model to fine-tune.** The
  same Sinhala study fine-tuned DeepSeek-OCR v1 and LightOnOCR-2-1B the same
  way. v1 reached 3.02% and LightOnOCR-2-1B reached 1.05%. BanglaWild
  (arXiv 2608.03884) found the same pattern for LoRA: it fixes catastrophic
  failure, but it doesn't raise the ceiling of models that are already
  competent.
- **Recommendation:** run a cheap pilot (below) with LightOnOCR-2-1B as a
  control arm. Don't promise a state-of-the-art result.

## Our starting point (local, measured)

| Item | Result |
|---|---|
| Zero-shot, Mozhi crops (37-item probe, 8-bit, 768 grid) | 238–446% CER. Output is LaTeX/Latin hallucination under all three prompts. |
| Zero-shot, REID pages on the RTX 2060 Super 8 GB | No successful page: OOM or timeout in every configuration. The pipeline's `cer 1.0` means 51/51 errors, not a measurement. |
| S0: `rifathridoy/bangla_deepseek_ocr_2` (see S0 below) | Fails. Fluent Bengali output, but unrelated to the image: 158% CER on the crop probe, 92% on 3 REID pages, and 0/20 exact on its own synthetic validation split. |
| Tokenizer | Byte-level BPE, lossless round-trip. About 860 merges decode to Bengali. Bengali averages 2.21 chars/token vs 5.47 for English, so sequences are about 2.5× longer. Not a blocker. |

## Evidence from comparable adaptations

| Study | Base model | Data | Before → after | Beat the best baseline? |
|---|---|---|---|---|
| Sinhala, arXiv 2606.29378 | DeepSeek-OCR-2, QLoRA | 707 real pages | 96.11% → 6.94% CER | Beat Surya (8.84%) and Tesseract (10.69%), but lost to v1 (3.02%) and LightOnOCR-2-1B (1.05%) |
| Persian, Unsloth DeepSeek-OCR-2 guide | DeepSeek-OCR-2, LoRA | synthetic crops (parsynth) | mean CER 4.19 → 0.60 (−86%) on 10 samples | Not evaluated. 10 samples is anecdotal. |
| Thai, arXiv 2609.03595 | PaddleOCR-VL-1.6 (0.9B) | 45,723 synthetic pages | median CER 6.64% → 1.24% (printed) | The authors report it outperforming baselines |
| Devanagari stress test, arXiv 2606.29213 | DeepSeek-OCR (zero-shot) | 300 real scans plus degradations | Best median of 10 systems, but "catastrophic repetition failures (up to 71× reference length)" | n/a |
| DeepSeek-OCR priors, arXiv 2601.03714 | DeepSeek-OCR (zero-shot) | semantically corrupted text | about 90% → 20% accuracy without language priors | n/a |

**Existing Bangla fine-tunes on Hugging Face.** Neither publishes any CER
figure:
- `rifathridoy/bangla_deepseek_ocr_2`: Unsloth merge, Apache-2.0. We probed
  it in S0 and it does not read Bengali (details under S0).
- `NafisAshraf/deepseek_ocr-synthdog_bangla_100k-fft`: DeepSeek-OCR-2
  architecture, full fine-tune on SynthDoG-Bangla, no licence.

## Training data

| Source | Level | Size | Licence | Notes |
|---|---|---|---|---|
| Mozhi-Bengali train | word crop | 80,113 | CC BY 4.0 | Not downloaded yet (we only have test/val). Same distribution as our test set, so tune on val only and report test once. |
| `rifathridoy/bengali-ocr-synthetic` | crop/line | 10K–100K | CC BY 4.0 | HarfBuzz shaping, VLM-ready. |
| `arobin79/bangla-ocr-validation_data_printed` | printed | 1K–10K | none declared | Usable only with the author's permission. |
| Synthetic pages from IndicCorp-bn / Wikipedia-bn | page | unlimited | per corpus | Need a HarfBuzz/raqm renderer. In the Thai study, typeface diversity and 2-D layout mattered most. |
| REID2019 non-test pages | page | about 30 | research | Far too few. The Sinhala study used 707 real pages. |

The gap is **real page-level printed Bengali ground truth**. Our in-house
annotation study could supply it, but only under its own data-release rules.
In-house pages must never reach public artifacts.

## Compute

- **Local RTX 2060 Super, 8 GB, Turing:** no fine-tuning recipe documents 8 GB,
  and inference alone already needs 8-bit weights. Local training is not
  realistic.
- **Free Colab/Kaggle T4, 16 GB:** Unsloth's DeepSeek-OCR-2 notebook targets
  the T4, with an fp16 fallback (`fp16 = not is_bf16_supported()`). This is
  the practical option.
- **ms-swift:** the DeepSeek-OCR v1 LoRA example needs 24 GiB. DeepSeek-OCR-2
  support was merged in PR #7917 on 2026-01-27, but there's no v2 example yet.
- **LLaMA-Factory:** no DeepSeek-OCR template.
- **Training time:** no source reports GPU-hours per epoch.

## Risks

- **Rare catastrophic repetition** wrecks micro-CER even when the median is
  good. Also report median CER and the catastrophic rate (output longer than
  2× the reference).
- **Leakage:** Mozhi train, val and test share a distribution. Keep test
  untouched until the final run.
- **Our results may not be comparable to published ones.** Many papers report
  median, BLEU or ANLS; our paper uses micro CER on NFC-normalised text.

## Staged plan with go/no-go

1. **S0: probe an existing Bangla fine-tune** (local, once the pipeline frees
   the GPU). Run `rifathridoy/bangla_deepseek_ocr_2` on the 37-crop probe and
   5 REID pages. **Go if:** crop CER is under 50% and there's no LaTeX output.

   **Result (2026-09-11): no-go for this checkpoint.** Setup: official
   DeepSeek-OCR-2 code with the fine-tune's weights (same 2,707 tensor names,
   identical tokenizer), 8-bit language model, base 1024 / image 768, crop
   mode. The uploader's own modeling code was not run: it swaps Unsloth's
   `ast.literal_eval` for `eval()` on model output.

   | Test | Fine-tune | Base (same config) | Reference |
   |---|---|---|---|
   | 37 Mozhi crops, "Free OCR." | 158.3% CER, 0 exact | 365.5% | bbOCR 1.0% |
   | 37 Mozhi crops, training prompt "OCR this Bengali text." | 115.5% CER, 0 exact | — | — |
   | 20 crops from its own synthetic validation split, training prompt | 100.6% CER, 0/20 exact | — | — |
   | 3 REID pages, no crop mode | 92.4% CER | out of memory | Surya 8.6%, EasyOCR 13.2%, Tesseract 28.1% |
   | 5 rendered English crops (pipeline control) | 5/5 exact | 5/5 exact | — |

   The English control rules out our load path. The cause is in the weights:
   only the language model changed (attention and MoE experts, about 0.3%
   relative), while all 298 vision-encoder tensors and the projector are
   bit-identical to the base model. A language-only LoRA taught the decoder to
   write Bengali, not the encoder to see it. This does not block S1, but it
   sets a hard requirement for it. Artifacts are in
   `pdf-craft-output/agents/probe_s0_rifathridoy/`.
2. **S1: word-crop LoRA** (Colab T4, 1–2 h). Use the Unsloth v2 notebook with
   2k Mozhi-train crops plus 2k synthetic crops, 60–500 steps. **Train the
   vision layers too** (`finetune_vision_layers=True`); S0 shows a
   language-only LoRA does not learn to read. Evaluate on
   Mozhi **val**. **Go if:** CER is under 13.4% (beats Surya on crops).
   **Stretch:** under 1.3%.
3. **S2: page-level LoRA.** Train on 10–45k synthetic HarfBuzz pages plus the
   REID non-test pages, and evaluate on the 51 REID test pages. **Go if:**
   micro-CER is under 15.7% (beats Surya) with a catastrophic rate under 5%.
4. **S3: control arm.** Repeat S1 and S2 with LightOnOCR-2-1B. If it wins
   (as it did on Sinhala), that finding goes in the paper.
5. **Integration.** Add a `deepseekocr_ft` engine (adapter path in
   `workflow/config.yaml`) so the paper's numbers regenerate automatically.

## Sources

- arXiv 2606.29378, *Cross-Temporal Sinhala OCR* (abstract and Table IV)
- arXiv 2609.03595, *How Far Can Synthetic Data Take Thai OCR?*
- arXiv 2606.29213, *Can OCR-VLMs Read Devanagari?*
- arXiv 2601.03714, *Visual Merit or Linguistic Crutch? A Close Look at DeepSeek-OCR*
- arXiv 2608.03884, BanglaWild
- https://unsloth.ai/docs/models/tutorials/deepseek-ocr-2
- github.com/unslothai/notebooks: `Deepseek_OCR_2_(3B).ipynb`
- github.com/modelscope/ms-swift, PR #7917
- Hugging Face API model cards for the two Bangla fine-tunes above

Raw retrieved files: `pdf-craft-output/agents/research_{a,b}/` (not committed).
