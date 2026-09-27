"""The leak harness's matcher decides what counts as a surviving value, so it gets its own tests.

Each "still a leak" case below is a way a real redactor could fail while a naive matcher
reports success.
"""

import pytest
import run as leak


def survives(entity_type: str, value: str, text: str, source: str = "out.md:text") -> bool:
    """Mirror run(): the whole value (fuzzy), or any identifying part (whole word)."""
    whole = leak.survives(value, source, leak.normalize(text))
    return whole or any(leak.words(p) in leak.words(text) for p in leak.parts(entity_type, value))


@pytest.mark.parametrize(
    ("entity_type", "value", "text"),
    [
        ("CREDIT_CARD", "4111 1111 1111 1111", "card: 4111-1111\n1111 1111 thanks"),
        ("PERSON", "Priya Okafor", "Contact Priya 0kafor today"),  # one OCR error
        ("PERSON", "Priya Okafor", "Contact [PERSON_1] Okafor today"),  # first name only
        ("PERSON", "Siobhan O'Brien", "Dear Siobhan O&#x27;Brien,"),  # HTML-escaped output
        ("EMAIL", "priya.okafor@example.com", "mail priya.okafor&#64;example.com"),
        ("PERSON", "Zoë Hart", "Zoë Hart"),  # decomposed accent (NFD)
        ("CA_SIN", "046 454 286", "SIN ０４６ 454 286"),  # fullwidth digits
        ("ADDRESS", "123 Maple Crescent, Springfield, IL 62704", "[NUM] Maple Crescent, [CITY]"),
        ("API_KEY", "-----BEGIN PRIVATE KEY-----\nAbCdEfGh12345678+/\n-----END PRIVATE KEY-----",
         "[API_KEY]\nAbCdEfGh12345678+/\n[API_KEY]"),  # only the armour lines were matched
    ],
)
def test_still_a_leak(entity_type, value, text):
    assert survives(entity_type, value, text)


def test_ocr_gets_more_edits_and_folds_look_alikes():
    iban, misread = "GB82WEST12345698765432", "G882WEST1234S698765432"  # B->8, 5->S
    assert not survives("IBAN", iban, misread, source="out.png:bytes")
    assert survives("IBAN", iban, misread, source="out.png:image_ocr")


@pytest.mark.parametrize(
    ("entity_type", "value", "text"),
    [
        ("PERSON", "Priya Okafor", "Contact [PERSON_1] today"),
        ("PERSON", "Priya Okafor", "Contact Pria 0kafor today"),  # two edits, not OCR
        ("PERSON", "Dr. Priya Okafor", "Dr. [PERSON_1] replied"),  # honorific is not a part
        ("CREDIT_CARD", "4111 1111 1111 1234", "card **** **** **** 1234"),  # last-four mask
        ("PASSPORT", "AB12", "ref ab13"),  # short values need an exact hit
    ],
)
def test_not_a_leak(entity_type, value, text):
    assert not survives(entity_type, value, text)
