"""Optional visual proofreading: the model sees the source paragraph crop."""

import base64
import io
import json
import re

from PIL import Image

from pdf_craft.extractor.chapter import create_chapters_reader, ParagraphLayout
from pdf_craft.pdf.handler import DefaultPDFHandler


class VisionProofreadingClient:
    def __init__(self, client, source, extraction, source_hash: str, raw_hash: str):
        self.client = client
        self.identity = f"{client.identity}:vision-crop-v1:{source_hash}:{raw_hash}"
        self.source = source
        self.dpi = extraction.render_dpi()
        self.boxes = {}
        with extraction._materialize() as paths:
            for chapter in create_chapters_reader(paths.chapters)():
                for layout in chapter.layouts:
                    if isinstance(layout, ParagraphLayout):
                        for block in layout.blocks:
                            self.boxes[(block.page_index, block.order)] = block.det
        self.document = None
        self.page_index = None
        self.image = None

    def __call__(self, system: str, user: str) -> str:
        payload = json.loads(user)
        match = re.fullmatch(r"p(\d+)-b(\d+)", payload["id"])
        if match is None:
            raise ValueError("Visual proofreading requires a source block ID")
        page, order = map(int, match.groups())
        det = self.boxes[(page, order)]
        if self.document is None:
            self.document = DefaultPDFHandler().open(self.source)
        if self.page_index != page:
            if self.image is not None:
                self.image.close()
            self.image = self.document.render_page(page, self.dpi)
            self.page_index = page
        crop = self.image.crop((max(0, det[0] - 12), max(0, det[1] - 12),
                                min(self.image.width, det[2] + 12), min(self.image.height, det[3] + 12)))
        crop.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        crop.save(stream, format="PNG")
        image = base64.b64encode(stream.getvalue()).decode("ascii")
        instruction = system + "\nThe attached image is the original paragraph. Read its glyphs carefully. Propose an edit only if the image clearly contradicts the OCR text. Preserve historical spellings visible in the image."
        return self.client.chat(instruction, user, images=[image])

    def close(self):
        if self.document is not None:
            self.document.close()
        if self.image is not None:
            self.image.close()
