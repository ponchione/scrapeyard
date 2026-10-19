"""Content-Encoding decoders that inflate a body a bounded piece at a time.

httpx decodes each network read in one call with no output limit, so a
response of about 2 KB in brotli, or in gzip inside gzip, became gigabytes in
memory before the run's byte budget saw any of it. These decoders follow
httpx's choice of codings but yield at most about ``PIECE_BYTES`` per step
(zstd about 2 MiB), so the caller charges the budget as the body inflates.
Under stacked codings every coding's output is handed out to be charged, not
only the body's.
"""

from __future__ import annotations

import importlib
import zlib
from collections.abc import Callable, Iterator, Sequence
from types import ModuleType
from typing import Any, Protocol

import httpx

PIECE_BYTES = 64 * 1024
# zstandard's streaming object has no output limit. A block takes at least
# four bytes and inflates to at most 128 KiB, so 64-byte slices bound one
# call's output to about 2 MiB.
_ZSTD_SLICE_BYTES = 64
# RFC 9659 caps a zstd content coding's window at 8 MB; browsers refuse more.
_ZSTD_MAX_WINDOW_BYTES = 8 * 1024 * 1024
# Each coding is another full pass over the body. Nothing real stacks more.
MAX_CONTENT_CODINGS = 4


def _optional_module(name: str) -> ModuleType | None:
    # httpx decodes br and zstd only when these are installed; so do we.
    try:
        return importlib.import_module(name)
    except ImportError:  # pragma: no cover - both are in the lock
        return None


_brotli = _optional_module("brotli")
_zstandard = _optional_module("zstandard")


class _CodingDecoder(Protocol):
    def decode(self, data: bytes) -> Iterator[bytes]: ...

    def flush(self) -> Iterator[bytes]: ...


class _IdentityDecoder:
    def decode(self, data: bytes) -> Iterator[bytes]:
        if data:
            yield data

    def flush(self) -> Iterator[bytes]:
        yield from ()


class _ZlibDecoder:
    """gzip; or deflate, zlib-wrapped or, failing that on the first bytes, raw."""

    def __init__(self, *, gzip: bool) -> None:
        self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16 if gzip else zlib.MAX_WBITS)
        self._raw_fallback = not gzip

    def decode(self, data: bytes) -> Iterator[bytes]:
        # Input after the stream's end is ignored, as httpx ignores it; zlib
        # would keep it, copying all it kept on every call.
        while data and not self._decompressor.eof:
            try:
                piece = self._decompressor.decompress(data, PIECE_BYTES)
            except zlib.error as exc:
                if not self._raw_fallback:
                    raise httpx.DecodingError(str(exc)) from exc
                self._decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                self._raw_fallback = False
                continue
            self._raw_fallback = False
            # Input is left over only when the piece filled its limit.
            data = self._decompressor.unconsumed_tail
            if not piece:
                return
            yield piece

    def flush(self) -> Iterator[bytes]:
        try:
            piece = self._decompressor.flush()
        except zlib.error as exc:  # pragma: no cover - as in httpx
            raise httpx.DecodingError(str(exc)) from exc
        if piece:
            yield piece


class _BrotliDecoder:
    def __init__(self) -> None:
        assert _brotli is not None
        self._decompressor = _brotli.Decompressor()

    def decode(self, data: bytes) -> Iterator[bytes]:
        assert _brotli is not None
        if not data:
            return
        try:
            piece = self._decompressor.process(data, output_buffer_limit=PIECE_BYTES)
            # Output past the limit is held back, even when the decoder says
            # it can take more input; calls with none hand it out until one
            # returns nothing.
            while piece:
                yield piece
                piece = self._decompressor.process(b"", output_buffer_limit=PIECE_BYTES)
        except _brotli.error as exc:
            raise httpx.DecodingError(str(exc)) from exc

    def flush(self) -> Iterator[bytes]:
        # httpx checks nothing here for this package; a truncated stream ends short.
        yield from ()


class _ZstdDecoder:
    def __init__(self) -> None:
        self._decompressor = self._new()
        self._seen_data = False

    @staticmethod
    def _new() -> Any:
        assert _zstandard is not None
        return _zstandard.ZstdDecompressor(
            max_window_size=_ZSTD_MAX_WINDOW_BYTES,
        ).decompressobj()

    def decode(self, data: bytes) -> Iterator[bytes]:
        assert _zstandard is not None
        self._seen_data = True
        for start in range(0, len(data), _ZSTD_SLICE_BYTES):
            try:
                piece = self._decompressor.decompress(data[start:start + _ZSTD_SLICE_BYTES])
                # A body may hold several frames, as httpx accepts.
                while self._decompressor.eof and self._decompressor.unused_data:
                    unused = self._decompressor.unused_data
                    self._decompressor = self._new()
                    piece += self._decompressor.decompress(unused)
            except _zstandard.ZstdError as exc:
                raise httpx.DecodingError(str(exc)) from exc
            if piece:
                yield piece

    def flush(self) -> Iterator[bytes]:
        if self._seen_data and not self._decompressor.eof:
            raise httpx.DecodingError("Zstandard data is incomplete")
        yield from ()


class ContentDecoder:
    """Undoes a response's codings, in the reverse of the order they were applied.

    Each step yields ``(piece, kept)``: a piece of the body is kept, and a piece
    one coding hands to the next is not. Both were inflated, so the caller
    charges both. A coding that takes input and yields nothing (an ended
    stream, empty deflate blocks, brotli metadata, zstd skippable frames)
    would otherwise let the coding before it inflate unseen.
    """

    def __init__(self, decoders: Sequence[_CodingDecoder]) -> None:
        self._decoders = tuple(decoders) or (_IdentityDecoder(),)

    def decode(self, data: bytes) -> Iterator[tuple[bytes, bool]]:
        return self._through(0, self._decoders[0].decode(data))

    def flush(self) -> Iterator[tuple[bytes, bool]]:
        for index, decoder in enumerate(self._decoders):
            yield from self._through(index, decoder.flush())

    def _through(self, index: int, pieces: Iterator[bytes]) -> Iterator[tuple[bytes, bool]]:
        if index == len(self._decoders) - 1:
            for piece in pieces:
                yield piece, True
            return
        for piece in pieces:
            yield piece, False
            yield from self._through(index + 1, self._decoders[index + 1].decode(piece))


_DECODERS: dict[str, Callable[[], _CodingDecoder]] = {
    "gzip": lambda: _ZlibDecoder(gzip=True),
    "deflate": lambda: _ZlibDecoder(gzip=False),
}
if _brotli is not None:
    _DECODERS["br"] = _BrotliDecoder
if _zstandard is not None:
    _DECODERS["zstd"] = _ZstdDecoder


def content_decoder(headers: httpx.Headers) -> ContentDecoder:
    """Return the bounded decoder for a response's ``Content-Encoding``.

    Like httpx, an unknown coding is skipped and the body is passed on as it
    came. More than ``MAX_CONTENT_CODINGS`` codings is refused.
    """

    decoders = [
        _DECODERS[name]()
        for value in headers.get_list("content-encoding", split_commas=True)
        if (name := value.strip().lower()) in _DECODERS
    ]
    if len(decoders) > MAX_CONTENT_CODINGS:
        raise httpx.DecodingError(
            f"Response applies {len(decoders)} content codings; at most "
            f"{MAX_CONTENT_CODINGS} are decoded"
        )
    return ContentDecoder(decoders[::-1])
