from pathlib import Path

from tokenizers import Tokenizer

TOKENIZER_REVISION='5617a9f61b028005a4858fdac845db406aefb181'
TOKENIZER_SHA256='21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08'


def verify_tokenizer(path: Path) -> None:
    import hashlib
    if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=TOKENIZER_SHA256:
        raise ValueError('BGE tokenizer checksum mismatch')


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
