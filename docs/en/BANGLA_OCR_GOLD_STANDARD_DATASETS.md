# Human-reviewed “gold standard” Bangla OCR datasets

**Search completed: 2026-09-10**

## Short answer

Yes—human-reviewed Bangla OCR corpora exist. However, I could not verify one dataset that is simultaneously (a) openly downloadable, (b) large, (c) full-page printed Bangla, and (d) independently reviewed by multiple annotators with a documented quality-control protocol. The strongest candidates serve different purposes: Ali et al. describe a very large, carefully reviewed corpus, but its public release is not verifiable; REID2019 is openly available and page/layout-oriented, but small; Mozhi-Bengali is openly available and manually transcribed, but word-cropped rather than page-level.

## Comparison

| Resource | What is documented | Human review / quality evidence | Access and fit for pdf-craft |
|---|---|---|---|
| [Ali et al. (EMNLP 2023), “Gold Standard Bangla OCR Dataset”](https://aclanthology.org/2023.emnlp-industry.44/) | 4,116,073 annotated character/word images, spanning computer-composed, letterpress, typewriter, outdoor and handwritten material. | Three annotators label and one supervisor validates. The paper reports kappa scores of 0.91 (computer-composed), 0.93 (letterpress), and 0.78 (typewriter). | The paper says the ICT Division would publish the corpus, but also says access was minimal because of confidentiality. I found no verified public download. Excellent evidence that a large reviewed corpus exists; not presently a reproducible evaluation dependency. |
| [REID2019 / heiDATA record (DOI 10.11588/DATA/AIQSXL)](https://doi.org/10.11588/DATA/AIQSXL) ([competition page](https://www.primaresearch.org/REID2019/), [ICDAR paper](https://primaresearch.org/www/assets/papers/ICDAR2019_Clausner_REID2019.pdf)) | 81 historical printed Bengali pages from books dated approximately 1713–1914; PAGE XML includes regions, polygons, metadata, reading order and transcription. The competition record describes 25 example pages plus a 56-image evaluation set. | Transcriptions were supplied by the School of Cultural Texts and Records at Jadavpur University. The sources document provenance and objective evaluation, but I did not find a published multi-reviewer agreement statistic or detailed correction/QC protocol. | Public/open data (the heiDATA record identifies CC0/public-domain terms). Best available fit for full-page printed historical evaluation and layout-aware pipeline testing, but far too small to represent modern books or train a general OCR model. |
| [Mozhi-Bengali (IIIT/NLTM OCR)](https://ilocr.iiit.ac.in/dataset/4/) | 100,013 printed Bengali word crops sampled from 1,000 scanned book pages; 80,113 train, 9,787 validation and 10,113 test images. | The dataset page explicitly says ground-truth transcriptions were manually annotated. It does not document multiple independent reviewers, agreement scores or a page-level correction protocol. | CC BY 4.0 and downloadable. Useful for recognition/normalization smoke tests and training word recognition; not suitable as a full-page/layout gold standard because page images and layout ground truth are not supplied as the evaluation unit. |

## Adjacent handwritten resources

These are relevant for separating handwriting performance from printed-book OCR, but they are not substitutes for a printed Bangla benchmark:

- [BN-HTRd v4](https://data.mendeley.com/datasets/743k6dm543/4) is a document-level offline Bangla handwritten text-recognition resource: 786 full-page images from about 150 writers, with word/line/document annotations. The associated [paper](https://arxiv.org/abs/2206.08977) explains that source text from BBC Bangla was used to generate handwriting annotations. It is a handwriting dataset, not printed OCR, and the public description does not establish multi-reviewer gold-standard adjudication.
- [ICDAR 2024 HWD](https://ilocr.iiit.ac.in/icdar_2024_hwd/dataset.html) includes Bengali alongside English, Hindi and Telugu. Its page states that handwritten page images were collected from native writers and manually annotated with word boxes, reading order and transcriptions; it provides page-level and isolated-word recognition tasks. It is useful for handwritten robustness checks, but not for printed-book OCR.

## Important exclusions and caveats

Layout-only resources such as [BaDLAD](https://bengaliai.github.io/badlad) can help evaluate document/layout detection: BaDLAD has human annotations for text boxes, paragraphs, images and tables, but not text transcriptions, so it is not OCR text ground truth. Synthetic images and isolated-character collections (for example, the isolated-character datasets discussed in the Ali et al. literature review) do not test continuous text, page segmentation, reading order or historical print variation. I also excluded self-published or unverified dataset claims where I could not locate a stable primary paper, repository record, license, annotation description and downloadable artifact.

The term “gold standard” is used inconsistently. Human transcription alone proves manual annotation, not necessarily independent double entry, adjudication, inter-annotator agreement, or a documented error taxonomy. A license for source scans also does not automatically license newly produced transcriptions; check the dataset record and underlying book rights before redistribution.

The April 2025 draft [Bangladesh Artificial Intelligence Readiness Assessment Report](https://objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/axvjbnqprylg/b/V2Ministry/o/office-ictd/2024/12/fb7f76c2185d4991b8f43f700c36a5a0.pdf) says that EBLICT's Bangla-language tools, including its OCR service, were online but its datasets remained unavailable. The current [ICT Division/BCC OCR service](https://ocr.bangla.gov.bd/) exposes document recognition, not a corpus download. Together with the absence of a dataset URL or license in the 2023 paper, this supports the cautious conclusion that the Ali et al. corpus was not verifiably public as of the search date.

## Recommendation for pdf-craft

1. Use REID2019 as the primary page-level regression fixture: preserve the original scans, PAGE XML, transcription, source/license metadata and a fixed test subset. Evaluate both text recognition (CER/WER) and layout/reading order.
2. Use Mozhi-Bengali for inexpensive word-level normalization and OCR smoke tests, clearly labeling results as crop-level rather than page-level.
3. Treat Ali et al. as a lead to follow up with the ICT Division/EBLICT, not as an available dependency. If access is obtained, request the images, transcriptions, document-type labels, split policy, annotator instructions, raw agreement data and supervisor/adjudication records.
4. For a genuine pdf-craft “gold” benchmark, build a small held-out set of representative scanned pages with at least two independent Bangla transcriptions, a documented adjudication pass, Unicode normalization rules, layout/reading-order annotations and a published license. This would address the exact gap left by the currently verifiable resources.

## Sources checked

- Ali et al., ACL Anthology paper and PDF: [landing page](https://aclanthology.org/2023.emnlp-industry.44/) and [PDF](https://aclanthology.org/2023.emnlp-industry.44.pdf).
- REID2019 primary project page: [PRImA](https://www.primaresearch.org/REID2019/); public dataset record: [heiDATA](https://doi.org/10.11588/DATA/AIQSXL); paper/PAGE XML and Jadavpur provenance: [ICDAR paper PDF](https://primaresearch.org/www/assets/papers/ICDAR2019_Clausner_REID2019.pdf).
- [Mozhi-Bengali dataset page](https://ilocr.iiit.ac.in/dataset/4/).
- [BN-HTRd v4 repository record](https://data.mendeley.com/datasets/743k6dm543/4) and [paper](https://arxiv.org/abs/2206.08977).
- [ICDAR 2024 HWD dataset page](https://ilocr.iiit.ac.in/icdar_2024_hwd/dataset.html).
