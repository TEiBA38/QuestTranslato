
import zipfile

def search_guide_keys(jar_path, mod_name):
    print('\n--- ' + mod_name + ' ---')
    try:
        with zipfile.ZipFile(jar_path, 'r') as z:
            for n in z.namelist():
                if n.endswith('en_us.lang') or n.endswith('en_US.lang'):
                    content = z.read(n).decode('utf-8', errors='ignore')
                    guide_lines = [line.strip() for line in content.split('\n') if 'guide' in line.lower() or 'book' in line.lower()]
                    print('Found ' + str(len(guide_lines)) + ' lines with guide or book')
                    for line in guide_lines[:10]:
                        print('  ', line)
    except Exception as e:
        print('Error:', e)

search_guide_keys(r'D:\MODE\Instances\MeatballCraft Dimensional Ascension\mods\Cyclic-1.12.2-1.20.14.jar', 'Cyclic')
search_guide_keys(r'D:\MODE\Instances\MeatballCraft Dimensional Ascension\mods\modular-routers-1.12.2-3.3.0-33.jar', 'Modular Routers')
search_guide_keys(r'D:\MODE\Instances\MeatballCraft Dimensional Ascension\mods\BloodMagic-1.12.2-2.4.3-105.jar', 'BloodMagic')

