import json
import re
from io import BytesIO

from file_processors import extract_snbt_targets
from hqm_parser import HQMQuestConverter, filter_text
from translation_engines import is_code_or_id


def _normalize_text(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _has_hangul(text):
    for ch in str(text):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            return True
    return False


def _is_suspect_untranslated(source, translated):
    src = _normalize_text(source)
    dst = _normalize_text(translated)
    if not src:
        return False

    if src == dst and re.search(r"[A-Za-z]", src):
        return True

    if re.search(r"[A-Za-z]{4,}", src) and not _has_hangul(dst) and src == dst:
        return True

    return False


def _summarize_pairs(pairs):
    total = len(pairs)
    changed = 0
    unchanged = 0
    suspect = 0
    examples = []

    for source, translated in pairs:
        src = _normalize_text(source)
        dst = _normalize_text(translated)
        if not src:
            continue

        if src == dst:
            unchanged += 1
        else:
            changed += 1

        if _is_suspect_untranslated(src, dst):
            suspect += 1
            if len(examples) < 5:
                examples.append((src, dst))

    return {
        "total": total,
        "changed": changed,
        "unchanged": unchanged,
        "suspect_untranslated": suspect,
        "suspect_examples": examples,
    }


def _sample_pairs(pairs, max_samples):
    if max_samples is None or max_samples <= 0 or len(pairs) <= max_samples:
        return pairs
    if max_samples == 1:
        return [pairs[0]]

    # Evenly sample from start to end for representative quick checks.
    step = (len(pairs) - 1) / (max_samples - 1)
    sampled = []
    for i in range(max_samples):
        idx = int(round(i * step))
        sampled.append(pairs[idx])
    return sampled


def analyze_snbt_texts(source_content, translated_content, max_samples=400):
    _, src_targets = extract_snbt_targets(source_content)
    _, dst_targets = extract_snbt_targets(translated_content)

    size = min(len(src_targets), len(dst_targets))
    pairs = []
    for i in range(size):
        source_text = src_targets[i][2].replace('\\"', '"')
        translated_text = dst_targets[i][2].replace('\\"', '"')
        pairs.append((source_text, translated_text))

    sampled_pairs = _sample_pairs(pairs, max_samples)
    summary = _summarize_pairs(sampled_pairs)
    summary["raw_total"] = len(pairs)
    summary["sampled"] = len(sampled_pairs) != len(pairs)
    summary["sample_size"] = len(sampled_pairs)
    return summary


def _collect_json_strings(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                if value.strip() and not is_code_or_id(value):
                    out.append((str(key), value))
            elif isinstance(value, (dict, list)):
                _collect_json_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _collect_json_strings(item, out)


def analyze_json_data(source_data, translated_data, max_samples=600):
    src_items = []
    dst_items = []
    _collect_json_strings(source_data, src_items)
    _collect_json_strings(translated_data, dst_items)

    size = min(len(src_items), len(dst_items))
    pairs = [(src_items[i][1], dst_items[i][1]) for i in range(size)]
    sampled_pairs = _sample_pairs(pairs, max_samples)
    summary = _summarize_pairs(sampled_pairs)
    summary["raw_total"] = len(pairs)
    summary["sampled"] = len(sampled_pairs) != len(pairs)
    summary["sample_size"] = len(sampled_pairs)
    return summary


def analyze_hqm_bytes(source_bytes, translated_bytes, max_samples=500):
    converter = HQMQuestConverter()
    src_file = converter.read(BytesIO(source_bytes))
    dst_file = converter.read(BytesIO(translated_bytes))

    src_entries = {entry.key: entry.value for entry in src_file.text_entries("quest") if filter_text(entry.value)}
    dst_entries = {entry.key: entry.value for entry in dst_file.text_entries("quest") if filter_text(entry.value)}

    keys = [key for key in src_entries.keys() if key in dst_entries]
    pairs = [(src_entries[key], dst_entries[key]) for key in keys]
    sampled_pairs = _sample_pairs(pairs, max_samples)
    summary = _summarize_pairs(sampled_pairs)
    summary["raw_total"] = len(pairs)
    summary["sampled"] = len(sampled_pairs) != len(pairs)
    summary["sample_size"] = len(sampled_pairs)
    return summary


def render_review_report(title, items):
    total_all = 0
    changed_all = 0
    unchanged_all = 0
    suspect_all = 0

    lines = [
        title,
        "=" * len(title),
        "",
    ]

    for label, summary in items:
        total_all += summary["total"]
        changed_all += summary["changed"]
        unchanged_all += summary["unchanged"]
        suspect_all += summary["suspect_untranslated"]

        lines.append(f"[{label}]")
        lines.append(f"- total: {summary['total']}")
        raw_total = summary.get("raw_total")
        sample_size = summary.get("sample_size")
        if raw_total is not None:
            lines.append(f"- raw_total: {raw_total}")
        if summary.get("sampled"):
            lines.append(f"- sampled: true ({sample_size}/{raw_total})")
        else:
            lines.append("- sampled: false")
        lines.append(f"- changed: {summary['changed']}")
        lines.append(f"- unchanged: {summary['unchanged']}")
        lines.append(f"- suspect_untranslated: {summary['suspect_untranslated']}")

        if summary["suspect_examples"]:
            lines.append("- suspect_examples:")
            for src, dst in summary["suspect_examples"]:
                lines.append(f"  * source: {src}")
                lines.append(f"    translated: {dst}")

        lines.append("")

    lines.append("[overall]")
    lines.append(f"- total: {total_all}")
    lines.append(f"- changed: {changed_all}")
    lines.append(f"- unchanged: {unchanged_all}")
    lines.append(f"- suspect_untranslated: {suspect_all}")
    lines.append("")

    return "\n".join(lines)
