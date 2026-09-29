"""Unit tests for redactit.detect: one positive and one negative case per entity type,
an offline guarantee, and a corpus-wide coverage check for text/Markdown documents.
"""

from __future__ import annotations

import random
import string

import generate as gen
import pytest

from redactit.detect.registry import Detector

OUR_VOCAB = {
    "EMAIL",
    "PHONE",
    "DATE_OF_BIRTH",
    "CREDIT_CARD",
    "IBAN",
    "CA_SIN",
    "US_SSN",
    "UK_NINO",
    "PASSPORT",
    "API_KEY",
    "COMPANY_TERM",
}


@pytest.fixture(scope="module")
def det() -> Detector:
    return Detector(company_terms=["Project Bluefinch", "Acme Holdings"])


def _spans_of(detector: Detector, text: str, entity_type: str):
    return [s for s in detector.detect(text) if s.entity_type == entity_type]


def _covers(spans, text: str, value: str) -> bool:
    start = text.index(value)
    end = start + len(value)
    return any(s.start <= start and s.end >= end for s in spans)


# --- EMAIL -------------------------------------------------------------------


def test_email_detected(det):
    text = "Reach me at jane.doe@example.com please."
    assert _covers(_spans_of(det, text, "EMAIL"), text, "jane.doe@example.com")


def test_email_without_domain_not_detected(det):
    text = "Contact foo at bar dot com for help."
    assert _spans_of(det, text, "EMAIL") == []


# --- PHONE ---------------------------------------------------------------


def test_phone_detected(det):
    text = "Call 555-123-4567 today."
    assert _covers(_spans_of(det, text, "PHONE"), text, "555-123-4567")


def test_phone_negative(det):
    text = "The quick brown fox jumps over the lazy dog near the old oak tree."
    assert _spans_of(det, text, "PHONE") == []


# --- DATE_OF_BIRTH ---------------------------------------------------------


def test_date_of_birth_detected(det):
    text = "Date of birth on file: 1990-05-14."
    assert _covers(_spans_of(det, text, "DATE_OF_BIRTH"), text, "1990-05-14")


def test_date_of_birth_negative(det):
    text = "The invoice total was 100.00 dollars for order 1990."
    assert _spans_of(det, text, "DATE_OF_BIRTH") == []


# --- CREDIT_CARD (Luhn) -----------------------------------------------------


def test_credit_card_luhn_valid_is_validated(det):
    body = "453987001234"
    number = body + gen.luhn_check_digit(body)
    text = f"Card on file: {number}."
    spans = _spans_of(det, text, "CREDIT_CARD")
    assert _covers(spans, text, number)
    assert all(s.validated for s in spans)


def test_credit_card_luhn_invalid_not_detected(det):
    body = "453987001234"
    bad_check = str((int(gen.luhn_check_digit(body)) + 1) % 10)
    number = body + bad_check
    assert not gen.luhn_ok(number)
    text = f"Card on file: {number}."
    assert _spans_of(det, text, "CREDIT_CARD") == []


# --- IBAN (mod-97) ----------------------------------------------------------


def _sample_iban() -> str:
    bban = "NWBK" + "601613" + "31926819"
    return f"GB{gen.iban_check_digits('GB', bban)}{bban}"


def test_iban_valid_is_validated(det):
    iban = _sample_iban()
    assert gen.iban_mod97_ok(iban)
    text = f"My IBAN is {iban}."
    spans = _spans_of(det, text, "IBAN")
    assert _covers(spans, text, iban)
    assert all(s.validated for s in spans)


def test_iban_bad_checksum_not_detected(det):
    iban = _sample_iban()
    broken = iban[:2] + "00" + iban[4:]
    assert not gen.iban_mod97_ok(broken)
    text = f"My IBAN is {broken}."
    # Still masked by shape (often an OCR misread, and IBAN is locked), but never "validated".
    assert not [s for s in _spans_of(det, text, "IBAN") if s.validated]


# --- CA_SIN (Luhn + reserved first digit) -----------------------------------


def test_ca_sin_valid_is_validated(det):
    body = "41860913"
    sin = body + gen.luhn_check_digit(body)
    text = f"SIN on file: {sin}"
    spans = _spans_of(det, text, "CA_SIN")
    assert _covers(spans, text, sin)
    assert all(s.validated for s in spans)


def test_ca_sin_bad_checksum_not_detected(det):
    body = "41860913"
    bad_check = str((int(gen.luhn_check_digit(body)) + 1) % 10)
    sin = body + bad_check
    text = f"SIN on file: {sin}"
    assert _spans_of(det, text, "CA_SIN") == []


def test_ca_sin_reserved_first_digit_not_detected(det):
    # First digit 0 is reserved and never issued; Luhn still passes, so this isolates
    # the first-digit rule specifically (unlike the checksum test above).
    body = "01234567"
    sin = body + gen.luhn_check_digit(body)
    text = f"SIN on file: {sin}"
    assert _spans_of(det, text, "CA_SIN") == []


# --- US_SSN ------------------------------------------------------------------


def test_us_ssn_valid_detected(det):
    text = "SSN: 219-09-9999"
    assert _covers(_spans_of(det, text, "US_SSN"), text, "219-09-9999")


def test_us_ssn_reserved_area_not_detected(det):
    text = "SSN: 666-12-3456"
    assert _spans_of(det, text, "US_SSN") == []


# --- UK_NINO -----------------------------------------------------------------


def test_uk_nino_valid_detected(det):
    text = "NINO: AB123456C"
    assert _covers(_spans_of(det, text, "UK_NINO"), text, "AB123456C")


def test_uk_nino_bad_prefix_not_detected(det):
    text = "NINO: BG123456C"
    assert _spans_of(det, text, "UK_NINO") == []


# --- PASSPORT ----------------------------------------------------------------


def test_passport_ca_style_detected(det):
    text = "Passport number: AB123456"
    spans = _spans_of(det, text, "PASSPORT")
    assert _covers(spans, text, "AB123456")
    # No checksum exists, so a passport is never "validated"; the cue makes it confident.
    assert max(s.score for s in spans) >= 0.85 and not any(s.validated for s in spans)


def test_passport_too_short_not_detected(det):
    text = "Reference code: AB123 for your files."
    assert _spans_of(det, text, "PASSPORT") == []


# --- API_KEY ------------------------------------------------------------------
# Keys are built from random characters at runtime -- never a realistic literal in
# source, which is what GitHub's push-protection scans for.


def test_api_key_aws_detected(det):
    key = "AKIA" + "".join(random.choices(string.ascii_uppercase + string.digits, k=16))
    text = f'export AWS_ACCESS_KEY_ID="{key}"'
    spans = _spans_of(det, text, "API_KEY")
    assert _covers(spans, text, key)
    assert all(s.validated for s in spans)


def test_api_key_github_token_detected(det):
    key = "ghp_" + "".join(random.choices(string.ascii_letters + string.digits, k=36))
    text = f'export GITHUB_TOKEN="{key}"'
    assert _covers(_spans_of(det, text, "API_KEY"), text, key)


def test_api_key_pem_block_covers_begin_to_end(det):
    alphabet = string.ascii_letters + string.digits + "+/"
    body = "\n".join("".join(random.choices(alphabet, k=64)) for _ in range(4))
    key = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    text = f"Rotate this immediately:\n{key}\nDone."
    spans = _spans_of(det, text, "API_KEY")
    assert _covers(spans, text, key)


def test_api_key_negative(det):
    text = "The build id is deadbeefcafebabefeed1234 for this release."
    assert _spans_of(det, text, "API_KEY") == []


# --- COMPANY_TERM --------------------------------------------------------------


def test_company_term_exact_match(det):
    text = "We discussed Project Bluefinch today."
    spans = _spans_of(det, text, "COMPANY_TERM")
    assert _covers(spans, text, "Project Bluefinch")
    assert all(s.validated and s.score == 1.0 for s in spans)


def test_a_short_company_term_is_not_part_of_a_longer_word(det):
    short = Detector(company_terms=["Acme"])
    assert _spans_of(short, "Acmeville council met today.", "COMPANY_TERM") == []


def test_a_long_codename_is_found_glued_or_inflected(det):
    """OCR glues words, and "Project Bluefinches" still names the codename."""
    for text in ("We discussed Project Bluefinches today.", "Re:ProjectBluefinch status"):
        assert _spans_of(det, text, "COMPANY_TERM"), text


def test_company_term_longest_match_wins():
    local_det = Detector(company_terms=["Acme", "Acme Holdings"])
    text = "The Acme Holdings deal closed."
    spans = _spans_of(local_det, text, "COMPANY_TERM")
    assert _covers(spans, text, "Acme Holdings")
    assert not any(text[s.start : s.end] == "Acme" for s in spans)


# --- Offline guarantee ---------------------------------------------------------


def test_email_detection_survives_cold_tldextract_cache(tmp_path, monkeypatch):
    """EmailRecognizer validates domains via tldextract, which fetches the public suffix
    list on a cold cache. The whole suite already runs with --disable-socket; pointing
    TLDEXTRACT_CACHE at a brand-new empty directory proves there is no warm cache to
    fall back on either -- detection must still work without any network attempt.
    """
    monkeypatch.setenv("TLDEXTRACT_CACHE", str(tmp_path / "empty_tldextract_cache"))
    fresh = Detector(company_terms=[])
    text = "Contact us at person@example.com for details."
    assert _covers(_spans_of(fresh, text, "EMAIL"), text, "person@example.com")


# --- Corpus coverage (text/Markdown only) --------------------------------------


def test_corpus_seed7_txt_md_full_coverage(tmp_path):
    manifest = gen.generate(seed=7, out=tmp_path, per_variant=1)
    terms_file = tmp_path / "company_terms.txt"
    terms = [t for t in terms_file.read_text(encoding="utf-8").splitlines() if t]
    corpus_det = Detector(company_terms=terms)

    for doc in manifest["documents"]:
        if doc["format"] not in ("txt", "md"):
            continue
        text = (tmp_path / doc["file"]).read_text(encoding="utf-8")
        spans = corpus_det.detect(text)
        for entry in doc["seeded"]:
            entity_type = entry["entity_type"]
            if entity_type not in OUR_VOCAB:
                continue
            value = entry["value"]
            start = text.index(value)
            end = start + len(value)
            covered = any(
                s.entity_type == entity_type and s.start <= start and s.end >= end
                for s in spans
            )
            assert covered, f"{doc['file']}: {entity_type} {value!r} not fully covered"


@pytest.mark.parametrize(
    "phone",
    ["(890)283-0166x131", "983-016-6131", "+44(0)116 496 0233", "554-323-1948 x757",
     "1 (140) 611-4307", "001-202-555-0147", "+441514960543", "202.555.0147"],
)
def test_formatted_phones_score_above_the_default_threshold(det, phone):
    spans = _spans_of(det, f"Reach us at {phone} today.", "PHONE")
    assert any(det_s.score >= 0.8 for det_s in spans), spans


def test_bare_digit_phone_is_confident_next_to_a_contact_cue(det):
    text = "Please contact Christopher Murray at cm@example.com or 01174960825 about it."
    assert max(s.score for s in _spans_of(det, text, "PHONE")) >= 0.85


def test_dates_ip_addresses_and_versions_are_not_phones(det):
    assert _spans_of(det, "Released 1965-03-11 as v3.8.16 on host 192.168.1.100.", "PHONE") == []


def test_a_date_is_a_confident_birth_date_only_after_a_cue(det):
    cued = _spans_of(det, "Date of birth on file: 1965-03-11.", "DATE_OF_BIRTH")
    bare = _spans_of(det, "The meeting moved to 1965-03-11.", "DATE_OF_BIRTH")
    assert max(s.score for s in cued) >= 0.85
    assert all(s.score < 0.35 for s in bare)


@pytest.mark.parametrize(
    "addr", ["USNV Jackson, FPO AE 65210", "USS Miller\nFPO AP 34321", "PSC 1234, Box 5678, APO AA 12345",
             "Unit 4321 Box 8765, DPO AE 09876"],
)
def test_military_addresses_are_whole_address_spans(det, addr):
    text = f"Ship to {addr} by Friday."
    assert any(text[s.start:s.end] == addr for s in _spans_of(det, text, "ADDRESS"))


# --- Cases an independent review found surviving; each must now be detected. -------------

def _rand(alphabet: str, n: int) -> str:
    return "".join(random.Random(n).choice(alphabet) for _ in range(n))


@pytest.mark.parametrize("email", ["priya.okafor@corp.local", "dmitri.volkov@acme.internal"])
def test_emails_on_internal_domains(det, email):
    assert _covers(_spans_of(det, f"Mail {email} today.", "EMAIL"), f"Mail {email} today.", email)


@pytest.mark.parametrize("addr", [
    "55 Oak Lane, Leeds LS1 4AB",
    "17 Rue de la Paix, 75002 Paris, France",
    "221B Baker Street, Marylebone, London NW1 6XE",
    "Flat 4B, 1234 North Maple Street, Springfield",
])
def test_street_addresses_the_model_misses(det, addr):
    text = f"Address: {addr}"
    assert _covers(_spans_of(det, text, "ADDRESS"), text, addr)


@pytest.mark.parametrize("text, dob", [
    ("Date of birth: 12.03.1985", "12.03.1985"),
    ("DOB: March 12th, 1985", "March 12th, 1985"),
    ("Born 1985/03/12 in Leeds.", "1985/03/12"),
])
def test_birth_dates_in_more_formats(det, text, dob):
    spans = [s for s in _spans_of(det, text, "DATE_OF_BIRTH") if text[s.start:s.end] == dob]
    assert spans and spans[0].score >= 0.85


def test_birth_dates_in_every_row_of_a_dob_column(det):
    text = "| Name | DOB |\n|---|---|\n| A | 1985-03-12 |\n| B | 1990-01-02 |\n| C | 1991-02-03 |\n"
    confident = {text[s.start:s.end] for s in _spans_of(det, text, "DATE_OF_BIRTH") if s.score >= 0.85}
    assert confident == {"1985-03-12", "1990-01-02", "1991-02-03"}


@pytest.mark.parametrize("text, phone", [
    ("Call 555-0199 after six.", "555-0199"),
    ("Mobile +33 6 12 34 56 78 in Paris.", "+33 6 12 34 56 78"),
    ("Desk 416–555–0199 ext.", "416–555–0199"),
])
def test_more_phone_formats(det, text, phone):
    spans = [s for s in _spans_of(det, text, "PHONE") if s.score >= 0.8]
    assert _covers(spans, text, phone)


@pytest.mark.parametrize("secret", [
    "ghu_" + _rand(string.ascii_letters + string.digits, 36),
    "ghr_" + _rand(string.ascii_letters + string.digits, 37),
    "hf_" + _rand(string.ascii_letters + string.digits, 34),
    "glpat-" + _rand(string.ascii_letters + string.digits, 20),
])
def test_more_token_families(det, secret):
    assert _covers(_spans_of(det, f"token={secret}", "API_KEY"), f"token={secret}", secret)


def test_aws_secret_key_after_its_cue(det):
    secret = _rand(string.ascii_letters + string.digits + "/+", 40)
    text = f"aws_secret_access_key = {secret}"
    assert any(s.score >= 0.85 for s in _spans_of(det, text, "API_KEY") if text[s.start:s.end] == secret)


@pytest.mark.parametrize("block", [
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: 1\n\n" + _rand(string.ascii_letters, 64) + "\n-----END PGP PRIVATE KEY BLOCK-----",
    "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n\n" + _rand(string.ascii_letters, 64) + "\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\n" + _rand(string.ascii_letters, 64) + "\n",  # END line cut off
])
def test_private_key_blocks_including_headers_and_truncation(det, block):
    assert _covers(_spans_of(det, block, "API_KEY"), block, block.rstrip("\n"))


@pytest.mark.parametrize("text, value, entity", [
    ("passport: ab123456", "ab123456", "PASSPORT"),
    ("NI number AB-12-34-56-C", "AB-12-34-56-C", "UK_NINO"),
    ("SSN 123-45 6789 on file", "123-45 6789", "US_SSN"),
])
def test_id_formats_with_other_case_and_separators(det, text, value, entity):
    assert _covers(_spans_of(det, text, entity), text, value)


# --- Round-two review cases. ---------------------------------------------------------------

def test_birth_dates_in_a_table_without_leading_pipes(det):
    text = "Name | DOB | City\n--- | --- | ---\nDmitri Volkov | 1979-11-02 | Leeds\nAnn Lee | 1980-01-03 | York\n"
    confident = {text[s.start:s.end] for s in _spans_of(det, text, "DATE_OF_BIRTH") if s.score >= 0.85}
    assert confident == {"1979-11-02", "1980-01-03"}


def test_bare_nine_digit_ids_are_masked_with_or_without_a_cue(det):
    for text in ("Employee 4471, SS no. 219099999, starts Monday.", "Reference 219099999 on file."):
        spans = det.detect(text)
        assert any(text[s.start:s.end] == "219099999" and s.score >= 0.35 for s in spans), text


def test_email_on_an_internationalised_domain(det):
    text = "Write to priya.okafor@müller-bau.de today."
    assert _covers(_spans_of(det, text, "EMAIL"), text, "priya.okafor@müller-bau.de")


@pytest.mark.parametrize("addr", [
    "PO Box 4417, Station A, Toronto ON",
    "Unit 7, Harbourside Business Centre, Plymouth PL4 0RA",
])
def test_po_boxes_and_numbered_units(det, addr):
    text = f"Send to {addr}."
    assert _covers(_spans_of(det, text, "ADDRESS"), text, addr)


def test_each_pattern_recognizer_has_its_own_name(det):
    spans = det.detect("Mail a@b.co or call 202-555-0147.")
    assert {s.detector.split(".")[0] for s in spans} >= {"email_pattern", "phone_pattern"}


@pytest.mark.parametrize("text, card", [
    ("Card on file: 3724180\r\n57889143", "3724180\r\n57889143"),
    ("Card: 4111 1111\n1111 1111 thanks", "4111 1111\n1111 1111"),
])
def test_card_numbers_wrapped_across_a_line(det, text, card):
    spans = [s for s in _spans_of(det, text, "CREDIT_CARD") if s.validated]
    assert _covers(spans, text, card)


def test_wrapped_digits_that_fail_luhn_are_not_cards(det):
    assert not [s for s in _spans_of(det, "Order 1234567\n12345678", "CREDIT_CARD") if s.validated]


# --- OCR often drops the space between a label and its value. ------------------------------

@pytest.mark.parametrize("text, value, entity", [
    ("IBANGB82WEST12345698765432PPCF581535", "GB82WEST12345698765432", "IBAN"),
    ("IBANGB82WEST12345698765432PPCF581535", "CF581535", "PASSPORT"),
    ("Card4111111111111111thanks", "4111111111111111", "CREDIT_CARD"),
    ("SSN219-09-9999end", "219-09-9999", "US_SSN"),
    ("Phone5551234567", "5551234567", "PHONE"),
    ("NIAB123456C", "AB123456C", "UK_NINO"),
])
def test_values_glued_to_a_label(det, text, value, entity):
    assert _covers(_spans_of(det, text, entity), text, value)


def test_a_glued_iban_is_only_validated_when_it_passes_mod97(det):
    spans = [s for s in _spans_of(det, "REFGB82WEST12345698765433", "IBAN") if s.detector == "glued_iban"]
    assert spans and not any(s.validated for s in spans)


@pytest.mark.parametrize("text, addr", [
    ("Address: 2 Josh Plains, \r\nVanessafort, S6 5WJ", "2 Josh Plains, \r\nVanessafort, S6 5WJ"),
    ("1678WallerInlet,EastMatthew,SKR3P1B2", "1678WallerInlet,EastMatthew,SKR3P1B2"),
    ("Address: PSC 6319, Box 47\r\n75, APO AP 11657", "PSC 6319, Box 47\r\n75, APO AP 11657"),
])
def test_addresses_wrapped_or_glued_by_ocr(det, text, addr):
    assert _covers(_spans_of(det, text, "ADDRESS"), text, addr)
