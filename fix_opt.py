
import os
options_path = r'D:\MODE\Instances\MeatballCraft Dimensional Ascension\options.txt'
with open(options_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if line.startswith('resourcePacks:'):
        lines[i] = 'resourcePacks:[' + chr(34) + 'QuestTranslatorPro_Lang_Pack_FIXED.zip' + chr(34) + ',' + chr(34) + 'QuestTranslatorPro_Patchouli_Pack_FIXED.zip' + chr(34) + ']\n'
with open(options_path, 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('Done!')

