from constants import DEFAULT_GLOSSARY

class AppState:
    def __init__(self):
        self.cancel_requested = False
        self.scan_thread_active = False
        self.translated_history = {}
        self.glossaries_by_lang = {}
        self.glossary = DEFAULT_GLOSSARY.copy()

    def sync_glossary(self, lang):
        if lang not in self.glossaries_by_lang:
            if lang == "한국어 (Korean)":
                self.glossaries_by_lang[lang] = DEFAULT_GLOSSARY.copy()
            else:
                self.glossaries_by_lang[lang] = {}
        self.glossary = self.glossaries_by_lang[lang]
