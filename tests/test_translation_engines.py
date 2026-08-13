import unittest
import sys
import os

# 모듈을 가져올 수 있도록 경로 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from translation_engines import is_code_or_id, normalize_reference_text, apply_builtin_quest_style_translation

class TestTranslationEngines(unittest.TestCase):
    def test_is_code_or_id(self):
        # 빈 문자열 처리
        self.assertTrue(is_code_or_id(""))
        self.assertTrue(is_code_or_id("   "))
        self.assertTrue(is_code_or_id(None))
        
        # 콜론 포함이고 띄어쓰기가 없는 경우 (예: 마인크래프트 아이템 ID)
        self.assertTrue(is_code_or_id("minecraft:stone"))
        self.assertTrue(is_code_or_id("botania:mana_pool"))
        self.assertFalse(is_code_or_id("minecraft: stone")) # 띄어쓰기 포함 시 False
        
        # 숫자만 있는 경우
        self.assertTrue(is_code_or_id("123"))
        self.assertTrue(is_code_or_id("0"))
        
        # 16진수 해시/ID
        self.assertTrue(is_code_or_id("0123456789ABCDEF"))
        self.assertTrue(is_code_or_id("3ea9d530e0")) # 길이 10
        self.assertFalse(is_code_or_id("abc")) # 짧은 영문자는 일반 텍스트로 취급해야 함
        
        # 일반 텍스트
        self.assertFalse(is_code_or_id("Hello world"))
        self.assertFalse(is_code_or_id("퀘스트 완료"))

    def test_normalize_reference_text(self):
        self.assertEqual(normalize_reference_text(""), "")
        self.assertEqual(normalize_reference_text(None), "")
        self.assertEqual(normalize_reference_text("Hello\nWorld"), "Hello World")
        self.assertEqual(normalize_reference_text("  a   b  c "), "a b c")

    def test_apply_builtin_quest_style_translation(self):
        self.assertEqual(apply_builtin_quest_style_translation("Quest"), "퀘스트")
        self.assertEqual(apply_builtin_quest_style_translation(" Quests "), " 퀘스트 ")
        self.assertEqual(apply_builtin_quest_style_translation("Tasks"), "과제")
        self.assertEqual(apply_builtin_quest_style_translation("Not found"), "Not found")

if __name__ == '__main__':
    unittest.main()
