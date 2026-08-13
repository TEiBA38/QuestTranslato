import unittest
import sys
import os

# 모듈을 가져올 수 있도록 경로 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from file_processors import extract_snbt_targets

class TestFileProcessors(unittest.TestCase):
    def test_extract_snbt_targets(self):
        sample_snbt = """{
            id: "12345678"
            title: "Hello Quest"
            subtitle: "A short subtitle"
            description: [
                "This is line 1."
                "This is line 2."
                "Ignore me because minecraft:stone"
            ]
            dependencies: ["11112222"]
            icon: "minecraft:grass_block"
            tasks: [{
                id: "22223333"
                type: "item"
                item: "minecraft:dirt"
            }]
        }"""
        
        lines, targets = extract_snbt_targets(sample_snbt)
        # expected targets:
        # 1. title: "Hello Quest"
        # 2. subtitle: "A short subtitle"
        # 3. description: ["This is line 1."]
        # 4. description: ["This is line 2."]
        # (The "minecraft:stone" line is filtered out because it's considered code/ID if it matches `is_code_or_id`. Wait, it has spaces, so it might not be filtered unless the exact logic in is_code_or_id filters it.)
        
        extracted_texts = [t[2] for t in targets]
        self.assertIn("Hello Quest", extracted_texts)
        self.assertIn("A short subtitle", extracted_texts)
        self.assertIn("This is line 1.", extracted_texts)
        self.assertIn("This is line 2.", extracted_texts)
        
        # Check that ignored keys aren't captured as targets
        self.assertNotIn("minecraft:grass_block", extracted_texts) # icon
        self.assertNotIn("minecraft:dirt", extracted_texts) # item

if __name__ == '__main__':
    unittest.main()
