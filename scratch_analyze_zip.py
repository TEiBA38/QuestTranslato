# Comprehensive scan: find ALL mods with guidebook content
import zipfile, os, json, re, glob

mods_dir = r'D:\MODE\Instances\MeatballCraft Dimensional Ascension\mods'
results = []

for jar_path in sorted(glob.glob(os.path.join(mods_dir, '*.jar'))):
    jar_name = os.path.basename(jar_path)
    try:
        with zipfile.ZipFile(jar_path, 'r') as zf:
            all_names = zf.namelist()
            
            has_patchouli = any('patchouli_books/' in n.lower() for n in all_names)
            has_guideapi_data = any('guideapi/' in n.lower() and n.endswith(('.json','.lang')) for n in all_names)
            has_guideapi_code = any('guideapi' in n.lower() and n.endswith('.class') for n in all_names)
            
            # Custom manual/doc systems
            manual_files = [n for n in all_names if 
                          (('/manual/' in n.lower() or '/doc/' in n.lower() or '/docs/' in n.lower()) 
                           and n.endswith(('.json', '.md', '.txt'))
                           and 'models/' not in n.lower()
                           and 'recipes/' not in n.lower()
                           and 'blockstates/' not in n.lower())]
            
            # BuildCraft-style guides  
            bc_guides = [n for n in all_names if 'guide/' in n.lower() and n.endswith(('.md', '.json', '.txt'))
                        and 'models/' not in n.lower()]
            
            book_type = None
            book_count = 0
            
            if has_patchouli:
                pfiles = [n for n in all_names if 'patchouli_books/' in n.lower() and n.endswith('.json')
                         and 'models/' not in n.lower() and 'textures/' not in n.lower()]
                book_type = 'Patchouli'
                book_count = len(pfiles)
            elif has_guideapi_code:
                book_type = 'GuideAPI (code-generated)'
                book_count = 0  # Content is in .lang files, not separate book files
            elif manual_files:
                book_type = 'Custom Manual'
                book_count = len(manual_files)
            elif bc_guides:
                book_type = 'BuildCraft Guide'
                book_count = len(bc_guides)
                
            if book_type:
                results.append({
                    'jar': jar_name,
                    'type': book_type,
                    'count': book_count,
                    'captured': book_type == 'Patchouli',  # Only Patchouli is currently captured
                    'manual_files': manual_files[:3] if manual_files else [],
                })
    except:
        pass

# Summary
print('='*100)
print(f'TOTAL MODS WITH IN-GAME BOOKS: {len(results)}')
print('='*100)

captured = [r for r in results if r['captured']]
not_captured = [r for r in results if not r['captured']]

print(f'\n[OK] CAPTURED BY TRANSLATOR ({len(captured)} mods):')
for r in captured:
    print(f'  [{r["type"]:25s}] {r["jar"]:60s} ({r["count"]} pages)')

print(f'\n[XX] NOT CAPTURED ({len(not_captured)} mods):')
for r in not_captured:
    extra = ''
    if r['manual_files']:
        extra = f' | e.g. {r["manual_files"][0]}'
    print(f'  [{r["type"]:25s}] {r["jar"]:60s} ({r["count"]} files){extra}')
