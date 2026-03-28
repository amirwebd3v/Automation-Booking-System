import sys
import types

# Lightweight shim for `transformers` used only in tests.
# This avoids installing the heavy `transformers`/`torch` packages
# while still allowing tests to patch the classes they expect.
mod = types.ModuleType("transformers")


class TrOCRProcessor:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        raise RuntimeError("tests shim: TrOCRProcessor.from_pretrained called unexpectedly")


class VisionEncoderDecoderModel:
    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        raise RuntimeError("tests shim: VisionEncoderDecoderModel.from_pretrained called unexpectedly")


mod.TrOCRProcessor = TrOCRProcessor
mod.VisionEncoderDecoderModel = VisionEncoderDecoderModel

sys.modules.setdefault("transformers", mod)
