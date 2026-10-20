import unittest
from translation_word import sanitize_minecraft_formatting

class TestMinecraftFormattingSanitizer(unittest.TestCase):
    def test_statech_missing_color_and_reset(self):
        src = "1.21.1 brings a lot of changes to StaTech Industry. Most importantly, with the change of modloaders (from &eFabric&r to &dNeoforge&r) and version (&e1.19.2&r to &d1.21.1&r), the mod list has &achanged significantly&r. "
        tgt = "1.21.1 버전은 StaTech Industry에 많은 변화를 가져왔습니다. 가장 중요하게는 모드 로더(&eFabric&r에서 &dNeoforge&r로)와 버전(&e1.19.2&r에서 &e1.21.1&r로)의 변경에 따라 모드 목록이 &상당히 변경되었습니다&."
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertIn("&a상당히 변경되었습니다&r.", result)
        self.assertNotIn("&상당히", result)
        self.assertNotIn("되었습니다&.", result)

    def test_brackets_formatting(self):
        src = "Press &b[Shift]&r to open"
        tgt = "열려면 &[Shift]&.를 누르세요"
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertIn("&b[Shift]&r.", result)

    def test_rogue_ampersand_removal(self):
        src = "Simple plain sentence."
        tgt = "여기는 &상당히 평범한 &문장입니다."
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertEqual(result, "여기는 상당히 평범한 문장입니다.")

    def test_normal_text_ampersand_preserved(self):
        src = "Iron & Gold Ingot"
        tgt = "철 & 금 주괴"
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertEqual(result, "철 & 금 주괴")

    def test_section_symbol_restoration(self):
        src = "Inmate touched the §kunfortunate soul§r in cell"
        tgt = "수감자는 감옥의 §불쌍한 영혼§r을 만졌다"
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertIn("§k불쌍한 영혼§r", result)

    def test_logical_and_operator_preserved(self):
        src = "Run if (a && b) holds true."
        tgt = "(a && b)가 참일 때 실행하세요."
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertEqual(result, "(a && b)가 참일 때 실행하세요.")

    def test_hex_color_code_support(self):
        src = "&#5C5C5CGrey Text&r with &#A0DE45Green Highlight&r"
        tgt = "&회색 텍스트&. 및 &초록색 강조&."
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertIn("&#5C5C5C회색 텍스트&r", result)
        self.assertIn("&#A0DE45초록색 강조&r", result)

    def test_end_of_string_ampersand(self):
        src = "Press &"
        tgt = "누르세요 &"
        result = sanitize_minecraft_formatting(src, tgt)
        self.assertEqual(result, "누르세요 &")

if __name__ == '__main__':
    unittest.main()

