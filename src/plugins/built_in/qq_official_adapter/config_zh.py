"""Local display translations. Stored keys, values and validation stay unchanged."""
import copy
import json
from pathlib import Path


def translate_schema(schema, translation):
    result = copy.deepcopy(schema)
    if not result and translation.get('fallback_schema'):
        result = copy.deepcopy(translation['fallback_schema'])
    if not result:
        return result
    result.setdefault('plugin_info', {}).update(translation.get('plugin_info', {}))
    for key, section in result.get('sections', {}).items():
        section_patch = translation.get('sections', {}).get(key, {})
        for item in ('title', 'description'):
            if item in section_patch:
                section[item] = section_patch[item]
        localized = section.setdefault('i18n', {}).setdefault('zh_CN', {})
        localized.update({k: section[k] for k in ('title', 'description') if section.get(k)})
        for name, field in section.get('fields', {}).items():
            patch = section_patch.get('fields', {}).get(name, {})
            for item in ('label', 'description', 'hint', 'placeholder'):
                if item in patch:
                    field[item] = patch[item]
            localized = field.setdefault('i18n', {}).setdefault('zh_CN', {})
            localized.update({k: field[k] for k in ('label', 'description', 'hint', 'placeholder') if field.get(k)})
            localized.update(patch.get('choices', {}))
            for subkey, subpatch in patch.get('item_fields', {}).items():
                child = field.get('item_fields', {}).get(subkey)
                if child is not None:
                    for item in ('label', 'placeholder'):
                        if item in subpatch:
                            child[item] = subpatch[item]
                    child.setdefault('i18n', {}).setdefault('zh_CN', {}).update(subpatch.get('i18n', {}))
    # Custom search pages also expose navigation tabs and group headings.
    for tab in result.get('layout', {}).get('tabs', []):
        patch = translation.get('tabs', {}).get(str(tab.get('id') or tab.get('name') or ''), {})
        for key in ('title', 'label', 'description'):
            if key in patch:
                tab[key] = patch[key]
    return result


def install(plugin_class):
    if plugin_class.__dict__.get('_local_config_zh_installed', False):
        return
    translation = json.loads(Path(__file__).with_name('config_zh.json').read_text(encoding='utf-8'))
    original = plugin_class.build_config_schema.__func__

    @classmethod
    def build_config_schema(cls, **plugin_info):
        schema = original(cls, **plugin_info)
        localized = translate_schema(schema, translation)
        if not schema:
            localized['plugin_id'] = plugin_info.get('plugin_id', '')
            localized.setdefault('plugin_info', {}).update({
                'version': plugin_info.get('plugin_version', ''), 'author': plugin_info.get('plugin_author', '')})
        return localized

    plugin_class.build_config_schema = build_config_schema
    plugin_class._local_config_zh_installed = True
