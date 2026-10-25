from pathlib import Path

from tokenizers import Tokenizer


class BGETokenBudget:
    def __init__(self,tokenizer_path: Path,max_tokens: int=8192):
        self.tokenizer=Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.max_tokens=max_tokens

    def count(self,text: str) -> int:
        return len(self.tokenizer.encode(text,add_special_tokens=True).ids)

    def validate(self,text: str) -> None:
        if not text.strip() or self.count(text)>self.max_tokens:
            raise ValueError('embedding input is empty or exceeds token budget')
