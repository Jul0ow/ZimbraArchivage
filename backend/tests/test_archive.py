import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.archive import (
    MAX_ATTACHMENT_NAME_LENGTH,
    DateRange,
    _unique_name,
    _zip_date_time,
    attachment_file_name,
    eml_file_name,
    extract_attachment_files,
    folder_archive_path,
    sanitize_path_component,
)
from app.config import Settings
from app.zimbra import Folder, MessageSummary
from tests.fake_zimbra import REST_URL, SOAP_URL

PARIS = ZoneInfo("Europe/Paris")


def ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=PARIS).timestamp() * 1000)


@pytest.mark.parametrize(
    "moment, included",
    [
        (datetime(2024, 3, 15, 0, 0), True),
        (datetime(2024, 3, 15, 23, 59, 59, 999000), True),
        (datetime(2024, 3, 16, 0, 0), False),
        (datetime(2024, 3, 14, 23, 59, 59, 999000), False),
    ],
)
def test_date_range_bounds(moment: datetime, included: bool):
    period = DateRange(date(2024, 3, 15), date(2024, 3, 15), PARIS)

    assert period.contains(int(moment.replace(tzinfo=PARIS).timestamp() * 1000)) is included


@pytest.mark.parametrize("day", [date(2024, 3, 31), date(2024, 10, 27)])
def test_date_range_on_daylight_saving_days(day: date):
    # Jours de 23 h et de 25 h : seule l'heure locale compte
    period = DateRange(day, day, PARIS)

    assert period.contains(ms(day.year, day.month, day.day, 0, 0))
    assert period.contains(ms(day.year, day.month, day.day, 23, 59))
    next_day = day + timedelta(days=1)
    assert not period.contains(ms(next_day.year, next_day.month, next_day.day, 0, 0))


def test_date_range_without_bounds():
    assert DateRange(None, None, PARIS).contains(0)
    assert DateRange(None, None, PARIS).query_terms() == []


def message(subject: str = "Budget", message_id: str = "102", date_ms: int | None = None):
    return MessageSummary(message_id, "2", ms(2024, 3, 15, 9, 0) if date_ms is None else date_ms, 0, subject)


def test_eml_file_name_is_truncated_to_the_maximum_length():
    name = eml_file_name(message("x" * 200), PARIS, 50)

    assert len(name) == 50
    assert name.startswith("20240315_xxx")
    assert name.endswith("_102.eml")


def test_eml_file_name_with_empty_subject():
    assert eml_file_name(message(""), PARIS, 50) == "20240315__102.eml"


def test_eml_file_name_with_a_long_identifier():
    long_id = "b3c1e1f2-0000-4000-8000-123456789abc:12345"

    name = eml_file_name(message(message_id=long_id), PARIS, 50)

    assert len(name) <= 50
    # Empreinte stable : le même message garde le même nom
    assert name == eml_file_name(message(message_id=long_id), PARIS, 50)
    assert name != eml_file_name(message(message_id=long_id + "6"), PARIS, 50)


def test_eml_file_name_sanitizes_the_identifier():
    assert eml_file_name(message(message_id="a:1"), PARIS, 50).endswith("_a_1.eml")


def test_dates_before_1980_are_stored_as_1980():
    assert _zip_date_time(datetime(1970, 1, 1, tzinfo=PARIS)) == (1980, 1, 1, 0, 0, 0)


@pytest.mark.parametrize("length", [10, 999])
def test_file_name_length_out_of_bounds_is_rejected(length: int):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, zimbra_soap_url=SOAP_URL, zimbra_rest_url=REST_URL, max_file_name_length=length
        )


def test_unknown_time_zone_is_rejected():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, zimbra_soap_url=SOAP_URL, zimbra_rest_url=REST_URL, zone_id="Europe/Nulle-Part"
        )


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/Inbox/Projets: 2024", "Inbox/Projets_ 2024"),
        ("/../../etc", "_/_/etc"),
        ("/./a/..", "_/a/_"),
        ("/CON/nul.txt", "_CON/_nul.txt"),
    ],
)
def test_folder_archive_path(path: str, expected: str):
    assert folder_archive_path(Folder("1", "x", path)) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("devis.pdf", "devis.pdf"),
        ("..", "_"),
        ("..\\..\\x", "x"),
        ("/etc/x", "x"),
        ("C:\\x", "x"),
        ("a\r\nb", "a__b"),
        ("NUL", "_NUL"),
        ("   ", "defaut.bin"),
        (None, "defaut.bin"),
    ],
)
def test_attachment_file_name_stays_in_its_folder(name: str | None, expected: str):
    assert attachment_file_name(name, "defaut.bin") == expected


def test_long_attachment_names_keep_their_extension():
    name = attachment_file_name("a" * 300 + ".pdf", "d")

    assert len(name) == MAX_ATTACHMENT_NAME_LENGTH
    assert name.endswith("a.pdf")


def test_long_attachment_names_without_extension():
    assert attachment_file_name("a" * 300, "d") == "a" * MAX_ATTACHMENT_NAME_LENGTH


def test_unique_names():
    used: set[str] = set()

    names = [_unique_name(n, used) for n in ["devis.pdf", "DEVIS.pdf", "notes", "notes", ".htaccess"]]

    assert names == ["devis.pdf", "DEVIS (2).pdf", "notes", "notes (2)", ".htaccess"]


def test_sanitize_path_component():
    assert sanitize_path_component('a<b>c:"d"|e?f*') == "a_b_c__d__e_f_"
    assert sanitize_path_component("") == "_"


def attachments(eml: bytes) -> list[tuple[str, bytes]]:
    return list(extract_attachment_files(io.BytesIO(eml)))


def test_message_that_is_itself_an_attachment():
    eml = (
        b"Subject: Scan\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="scan.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"JVBERi0xLjQ=\r\n"
    )

    assert attachments(eml) == [("scan.pdf", b"%PDF-1.4")]


def test_simple_text_message_has_no_attachment():
    assert attachments(b"Subject: a\r\nContent-Type: text/plain\r\n\r\nBonjour\r\n") == []


def test_inline_named_image_and_encoded_name():
    eml = (
        b'Content-Type: multipart/related; boundary="b"\r\n\r\n'
        b"--b\r\n"
        b'Content-Type: multipart/alternative; boundary="c"\r\n\r\n'
        b"--c\r\nContent-Type: text/plain\r\n\r\ntexte\r\n"
        b"--c\r\nContent-Type: text/html\r\n\r\n<p>html</p>\r\n"
        b"--c--\r\n"
        b"--b\r\n"
        b'Content-Type: image/png; name="logo.png"\r\n'
        b'Content-Disposition: inline; filename="logo.png"\r\n\r\n'
        b"PNG\r\n"
        b"--b\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?b?w6l0w6kucGRm?="\r\n\r\n'
        b"pdf\r\n"
        b"--b--\r\n"
    )

    assert [name for name, _ in attachments(eml)] == ["logo.png", "été.pdf"]


def test_deeply_nested_mime_is_rejected():
    depth = 30
    eml = (
        b"".join(
            f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n'.encode() for i in range(depth)
        )
        + b"Content-Type: text/plain\r\n\r\nx\r\n"
    )

    with pytest.raises(ValueError):
        attachments(eml)
