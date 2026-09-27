# Phase 1 leak report: pass-through run

Corpus seed 1234, `--outputs` pointed at the unredacted inputs. This run passes when **every**
seeded value survives: it proves the harness can see leaks in every format before any
redactor is trusted. All 132 were found by a format-aware extractor (text, OOXML, PDF text
layer, 300 DPI OCR, image OCR, barcode or metadata), none by raw byte search alone. The 18
violations are the inputs' own text layers and metadata, also expected here.


Survivors: **132** of 132 seeded values.
Metadata / text-layer violations: **18**.

| Format / entity | Seeded | Removed | Recall |
|---|---:|---:|---:|
| docx/ADDRESS | 2 | 0 | 0% |
| docx/COMPANY_TERM | 2 | 0 | 0% |
| docx/CREDIT_CARD | 2 | 0 | 0% |
| docx/EMAIL | 4 | 0 | 0% |
| docx/IBAN | 2 | 0 | 0% |
| docx/PERSON | 16 | 0 | 0% |
| docx/UK_NINO | 2 | 0 | 0% |
| docx/US_SSN | 2 | 0 | 0% |
| jpg/ADDRESS | 2 | 0 | 0% |
| jpg/COMPANY_TERM | 2 | 0 | 0% |
| jpg/CREDIT_CARD | 2 | 0 | 0% |
| jpg/EMAIL | 4 | 0 | 0% |
| jpg/IBAN | 2 | 0 | 0% |
| jpg/PASSPORT | 2 | 0 | 0% |
| jpg/PERSON | 4 | 0 | 0% |
| jpg/PHONE | 4 | 0 | 0% |
| md/API_KEY | 2 | 0 | 0% |
| md/CA_SIN | 2 | 0 | 0% |
| md/COMPANY_TERM | 2 | 0 | 0% |
| md/CREDIT_CARD | 2 | 0 | 0% |
| md/EMAIL | 2 | 0 | 0% |
| md/PERSON | 4 | 0 | 0% |
| md/PHONE | 2 | 0 | 0% |
| pdf/ADDRESS | 2 | 0 | 0% |
| pdf/COMPANY_TERM | 2 | 0 | 0% |
| pdf/CREDIT_CARD | 4 | 0 | 0% |
| pdf/DATE_OF_BIRTH | 2 | 0 | 0% |
| pdf/EMAIL | 4 | 0 | 0% |
| pdf/PERSON | 8 | 0 | 0% |
| pdf/PHONE | 2 | 0 | 0% |
| pdf/US_SSN | 2 | 0 | 0% |
| png/ADDRESS | 2 | 0 | 0% |
| png/COMPANY_TERM | 2 | 0 | 0% |
| png/CREDIT_CARD | 2 | 0 | 0% |
| png/EMAIL | 4 | 0 | 0% |
| png/IBAN | 2 | 0 | 0% |
| png/PASSPORT | 2 | 0 | 0% |
| png/PERSON | 4 | 0 | 0% |
| png/PHONE | 4 | 0 | 0% |
| txt/ADDRESS | 2 | 0 | 0% |
| txt/COMPANY_TERM | 2 | 0 | 0% |
| txt/CREDIT_CARD | 2 | 0 | 0% |
| txt/DATE_OF_BIRTH | 2 | 0 | 0% |
| txt/EMAIL | 2 | 0 | 0% |
| txt/PERSON | 2 | 0 | 0% |
| txt/PHONE | 2 | 0 | 0% |

Precision: not measured (no --spans file)
