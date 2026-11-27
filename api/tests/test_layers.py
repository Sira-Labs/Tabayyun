"""Layer names of a read (spec 036), without a database."""

from __future__ import annotations

import pytest

from tabayyun.services.layers import RAW, LayerError, LayerRef, parse_layer


def test_parse_layer_names():
    """`raw` (also when nothing is given), `corrected@latest` and `corrected@v{n}`."""
    assert parse_layer(None) is RAW and parse_layer("raw") == RAW
    assert (RAW.name, RAW.kind, RAW.version, RAW.is_raw) == ("raw", "raw", None, True)
    latest = parse_layer("corrected@latest")
    assert latest == LayerRef(name="corrected@latest", kind="corrected", version=None) and not latest.is_raw
    assert parse_layer("corrected@v1") == LayerRef(name="corrected@v1", kind="corrected", version=1)
    assert parse_layer("corrected@v120").version == 120
    assert parse_layer("corrected@v2147483647").version == 2**31 - 1
    # Reads of several series take the names that mean the same for every series.
    assert parse_layer("raw", versions=False) == RAW
    assert parse_layer("corrected@latest", versions=False) == latest


@pytest.mark.parametrize(
    "name",
    [
        "",
        "RAW",
        " raw",
        "corrected",
        "corrected@",
        "corrected@v",
        "corrected@v0",
        "corrected@v01",
        "corrected@v-1",
        "corrected@v1.0",
        "corrected@v1 ",
        "corrected@V1",
        "corrected@1",
        "corrected@newest",
        "corrected/v1",
        "corrected@v2147483648",
        "corrected@v99999999999999999999",
        "published",
        "raw@latest",
    ],
)
def test_refused_names(name):
    """Anything else is refused with a message that names the three forms."""
    with pytest.raises(LayerError, match="raw, corrected@latest or corrected@v"):
        parse_layer(name)


def test_refused_names_for_several_series():
    """A version is refused where several series are read, and the message says why."""
    with pytest.raises(LayerError, match="versions count per series"):
        parse_layer("corrected@v1", versions=False)
    with pytest.raises(LayerError, match="raw, corrected@latest or corrected@v"):
        parse_layer("bogus", versions=False)
