"""Fixed research-schema aliases; self-created public Python fixture, no provider/DB/network."""
from copy import deepcopy
import json
import unittest

from services.wuyu_errors import PipelineError
from services.wuyu_deep_research import validate_report
from services.wuyu_json_normalization import canonical_schema_fields, normalized_fields, parse_object


class ResearchSchemaTests(unittest.TestCase):
    def plain_report(self):
        quote = 'Evidence 12.50% remains exactly as reported.'
        source = {'id': 'S1', 'url': 'https://example.test/self-public',
                  'text': quote + '\nThe source keeps 300 kg and source tokens.'}
        raw = {'summary': 'Self-created summary', 'findings': [
            {'statement': 'This self-created evidence keeps 12.50%.',
             'evidence': [{'source_id': 'S1', 'quote': quote}]}],
            'uncertainties': ['No independent verification'], 'conflicts': [],
            'suggested_sections': ['Evidence']}
        return raw, [source]

    def wrap_keys(self, value, ending='\n'):
        if isinstance(value, dict):
            return {key[:1] + ending + key[1:]: self.wrap_keys(child, ending)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [self.wrap_keys(child, ending) for child in value]
        return value

    def test_exact_persisted_self_fixture_recovers_only_the_grounded_finding(self):
        raw, sources = deepcopy(SELF_FIXTURE['synthesis']), deepcopy(SELF_FIXTURE['sources'])
        before = deepcopy(raw)
        result = validate_report(raw, sources)
        self.assertEqual(len(result['findings']), 1)
        evidence = result['findings'][0]['evidence'][0]
        self.assertEqual(evidence['source_id'], 'S7')
        self.assertEqual(evidence['quote'], raw['findings'][1]['evidence'][0]['\nquote'])
        self.assertEqual(result['omitted_findings'], 1)
        self.assertNotIn('summary_unverified', result)
        self.assertEqual(result['suggested_sections'], raw['suggested_sections\n'])
        self.assertEqual(raw, before)
        self.assertEqual(result['sources'], sources)
        self.assertFalse(result['fully_verified'])

    def test_raw_literal_crlf_keys_at_all_schema_levels_are_recovered(self):
        raw, sources = self.plain_report()
        wrapped = self.wrap_keys(raw, '\r\n')
        # Only structural key tokens become literal CR/LF; values stay valid JSON data.
        wire = json.dumps(wrapped).replace('\\r\\n', '\r\n')
        parsed = parse_object(wire)
        self.assertEqual(validate_report(parsed, sources), validate_report(raw, sources))

    def test_escaped_lf_and_persisted_plain_keys_are_explicit_schema_aliases(self):
        raw, sources = self.plain_report()
        wrapped = self.wrap_keys(raw)
        parsed = parse_object(json.dumps(wrapped))
        expected = validate_report(raw, sources)
        self.assertEqual(validate_report(parsed, sources), expected)
        self.assertEqual(validate_report(json.loads(json.dumps(wrapped)), sources), expected)

    def test_normal_report_public_contract_and_all_input_values_remain_unchanged(self):
        raw, sources = self.plain_report()
        before, original_sources = deepcopy(raw), deepcopy(sources)
        result = validate_report(raw, sources)
        expected = {'summary': raw['summary'], 'findings': [
            {'statement': raw['findings'][0]['statement'], 'evidence': [
                {'source_id': 'S1', 'quote': raw['findings'][0]['evidence'][0]['quote'],
                 'url': sources[0]['url']}],
             'verification': 'source_quote_matched_not_independently_verified'}],
            'uncertainties': raw['uncertainties'], 'conflicts': [],
            'suggested_sections': raw['suggested_sections'], 'sources': sources,
            'omitted_findings': 0, 'coverage': 'bounded_public_source_review', 'fully_verified': False}
        self.assertEqual(result, expected)
        self.assertEqual(raw, before)
        self.assertEqual(sources, original_sources)

    def test_alias_collisions_fail_closed_at_root_finding_and_evidence_levels(self):
        raw, sources = self.plain_report()
        cases = []
        root = deepcopy(raw); root['find\nings'] = deepcopy(root['findings']); cases.append(root)
        finding = deepcopy(raw); finding['findings'][0]['sta\ntement'] = 'conflicting'; cases.append(finding)
        evidence = deepcopy(raw); evidence['findings'][0]['evidence'][0]['\nquote'] = 'conflicting'; cases.append(evidence)
        two_aliases = deepcopy(raw); ref = two_aliases['findings'][0]['evidence'][0]
        ref['\nquote'] = ref.pop('quote'); ref['quo\rte'] = 'conflicting'; cases.append(two_aliases)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                before = deepcopy(candidate)
                with self.assertRaises(PipelineError) as error:
                    validate_report(candidate, sources)
                self.assertEqual(error.exception.code, 'research_report_invalid')
                self.assertEqual(candidate, before)

    def test_unknown_case_space_tab_and_backslash_n_keys_are_not_repaired(self):
        values = {'un\nknown': {'keep': 12.50}, 'quo te': 'space', 'quo\tte': 'tab',
                  'Quote': 'case', r'quo\nte': 'backslash n'}
        result = canonical_schema_fields(values, {'quote'})
        self.assertEqual(result, values)
        self.assertIs(result['un\nknown'], values['un\nknown'])
        raw, sources = self.plain_report()
        raw['findings'][0]['evidence'][0]['quo te'] = raw['findings'][0]['evidence'][0].pop('quote')
        self.assertEqual(validate_report(raw, sources)['findings'], [])

    def test_alias_conflicts_with_another_whitespace_key_are_rejected(self):
        for other in ('quote', 'quo te', 'quo\tte', '\rquote'):
            with self.subTest(other=other), self.assertRaises(PipelineError):
                canonical_schema_fields({'\nquote': 'one', other: 'two'}, {'quote'}, 'research_report_invalid')

    def test_values_source_ids_paths_numbers_and_quoted_text_are_never_canonicalized(self):
        value = {'source_id': 'S\n1', 'quote': 'Value 12.50%\n300 kg',
                 'statement': 'a\nb', 'number': '1\n2', 'path': '/tmp/quo\nte.json',
                 'sha256': 'a\nb', 'url': 'https://example.test/a%0Ab'}
        result = canonical_schema_fields({'\nevidence': value}, {'evidence'})
        self.assertIs(result['evidence'], value)
        self.assertEqual(result['evidence'], value)
        raw, sources = self.plain_report()
        raw['findings'][0]['evidence'][0]['source_id'] = 'S\n1'
        self.assertEqual(validate_report(raw, sources)['findings'], [])

    def test_wrong_source_numeric_changes_and_spliced_quotes_remain_rejected(self):
        raw, sources = self.plain_report()
        for change in ('Evidence 12.51% remains exactly as reported.',
                       'Evidence 1250% remains exactly as reported.',
                       'Evidence 12.50%...The source keeps 300 kg'):
            with self.subTest(quote=change):
                candidate = deepcopy(raw)
                candidate['findings'][0]['evidence'][0] = {'source_id': 'S1', '\nquote': change}
                result = validate_report(candidate, sources)
                self.assertEqual(result['findings'], [])
                self.assertTrue(result['summary_unverified'])
        raw['findings'][0]['evidence'][0]['source_id'] = 'unknown'
        self.assertEqual(validate_report(raw, sources)['findings'], [])

    def test_existing_default_normalizer_keeps_its_literal_provenance_boundary(self):
        plain = {'\nquote': 'verbatim value'}
        escaped = parse_object(json.dumps(plain))
        literal = parse_object('{"\nquote":"verbatim value"}')
        self.assertEqual(normalized_fields(plain, {'quote'}), plain)
        self.assertEqual(normalized_fields(escaped, {'quote'}), plain)
        self.assertEqual(normalized_fields(literal, {'quote'}), {'quote': 'verbatim value'})
        self.assertEqual(canonical_schema_fields(plain, {'quote'}), {'quote': 'verbatim value'})


# Exact parsed synthesis and its two bound sources from the sole synthetic124 run.
# This is not original wire JSON; persistence did not retain literal-break provenance.
SELF_FIXTURE = {'synthesis': {'summary': '在Python开发中，虚拟环境的创建与激活是两个不同阶段\n'
                          '的操作。创建（Creation）侧重于环境的构建与初始化，即将解释器及相关依赖目录隔离至\n'
                          '指定文件夹；而激活（Activation）则侧重于运行时环境的切换，通过修改当前终端的Shell配置（\n'
                          '如PATH变量、提示符显示），确保后续命令（如pip、python）优先指向该环境下的二进制文件与库\n'
                          '，从而实现项目间的环境隔离。',
               'findings': [{'evidence': [{'quote': '虚拟\n'
                                                    '环境 是通过执行venv 模块来创建的：python -m venv '
                                                    '/path/to/new/virtual/environment 此\n'
                                                    '命令会创建目标目录（必要时还包括父目录）并在其中放置一个pyvenv.cfg '
                                                    '文件...它\n'
                                                    '还会创建一个 bin 子目录 (在 Windows 上为 Scripts)，其中包含 '
                                                    'Python 可执行文件的副本或符号链接 (具体\n'
                                                    '由创建环境时所使用的平台或参数而定)。',
                                           'source_id': 'S11'}],
                             'statement': '虚拟\n环境的创建通过执行venv模块完成，旨在构建一个包含独立Python解释器及支持文件的目录结构。'},
                            {'evidence': [{'\nquote': '激活虚拟环境将改变你所用终端的提示符，以显示你正在使用的虚拟环境，并修改环境\n'
                                                      '以使python 命令所运行的将是已安装的特定 Python 版本。',
                                           'source_id': 'S7'}],
                             'statement': '虚拟环境的激活用于切换当前终端的运行时环境，使其能够直接调用虚拟环境内的\n特定解释器和库。'}],
               'conflicts': ['无实质性分歧，仅在版本演进过程中存在工具推荐的\n变迁（从早期的pyvenv脚本转向现在的python -m venv模块）。'],
               'uncertainties': ['虽然Python 3.3+提供了统一的venv工具\n'
                                 '，但旧版文档（如3.5及以下）中关于pyvenv脚本的描述与当前推荐的python\n'
                                 ' -m venv方式存在历史差异，需注意版本兼容性。',
                                 '不同Shell环境（bash,\n csh, fish）下激活脚本的具体路径与语法存在差异，文档未穷举所有平台边缘情况。'],
               'suggested_sections\n': ['Python虚拟环境机制：创建与运行时的本质区别',
                                        'venv工作流详解：从目录\n构建到环境加载',
                                        '多平台下的激活差异：Windows与Unix/macOS配置逻辑']},
 'sources': [{'id': 'S7',
              'doi': '',
              'url': 'https://docs.python.org/zh-cn/3.14/tutorial/venv.html',
              'date': '',
              'text': '# 12. 虚拟环境和包¶\n'
                      '\n'
                      '## 12.1. 概述¶\n'
                      '\n'
                      'Python '
                      '应用程序通常会使用不在标准库内的软件包和模块。应用程序有时需要特定版本的库，因为应用程序可能需要已修复某个特定错误的库版本，或者应用程序可能是使用了某个库的旧版接口编写的。\n'
                      '\n'
                      '这意味着一个 Python '
                      '安装可能无法满足每个应用程序的要求。如果应用程序A需要特定模块的1.0版本但应用程序B需要2.0版本，则需求存在冲突，安装版本1.0或2.0将导致某一个应用程序无法运行。\n'
                      '\n'
                      '这个问题的解决方案是创建一个 virtual environment，一个目录树，其中安装有特定 Python 版本，以及许多其他包。\n'
                      '\n'
                      '然后，不同的应用将可以使用不同的虚拟环境。 要解决先前需求相冲突的例子，应用程序 A 可以拥有自己的安装了 1.0 版本的虚拟环境，而应用程序 B '
                      '则拥有安装了 2.0 版本的另一个虚拟环境。 如果应用程序 B 要求将某个库升级到 3.0 版本，也不会影响应用程序 A 的环境。\n'
                      '\n'
                      '## 12.2. 创建虚拟环境¶\n'
                      '\n'
                      '用于创建和管理虚拟环境的模块是venv。 venv 将安装运行命令所使用的 Python 版本（即 --version 选项所报告的版本）。 '
                      '例如，使用 python3.12 执行命令将会安装 3.12 版。\n'
                      '\n'
                      '要创建虚拟环境，请确定要放置它的目录，并将venv 模块作为脚本运行目录路径:\n'
                      'python -m venv tutorial-env\n'
                      '\n'
                      '这将创建tutorial-env 目录，如果它不存在的话，并在其中创建包含 Python 解释器副本和各种支持文件的目录。\n'
                      '\n'
                      '虚拟环境的常用目录位置是.venv。 这个名称通常会令该目录在你的终端中保持隐藏，从而避免需要对所在目录进行额外解释的一般名称。 '
                      '它还能防止与某些工具所支持的 .env 环境变量定义文件发生冲突。\n'
                      '\n'
                      '创建虚拟环境后，您可以激活它。\n'
                      '\n'
                      '在Windows上，运行:\n'
                      'tutorial-env\\Scripts\\activate\n'
                      '\n'
                      '在Unix或MacOS上，运行:\n'
                      'source tutorial-env/bin/activate\n'
                      '\n'
                      '（这个脚本是为bash shell编写的。如果你使用 csh 或 fish shell，你应该改用activate.csh 或 '
                      'activate.fish 脚本。）\n'
                      '\n'
                      '激活虚拟环境将改变你所用终端的提示符，以显示你正在使用的虚拟环境，并修改环境以使python 命令所运行的将是已安装的特定 Python 版本。 '
                      '例如：\n'
                      '$ source ~/envs/tutorial-env/bin/activate\n'
                      '(tutorial-env) $ python\n'
                      'Python 3.5.1 (default, May  6 2016, 10:59:36)\n'
                      '  ...\n'
                      '>>> import sys\n'
                      '>>> sys.path\n'
                      "['', '/usr/local/lib/python35.zip', ...,\n"
                      "'~/envs/tutorial-env/lib/python3.5/site-packages']\n"
                      '>>>\n'
                      '\n'
                      '请注意激活的虚拟环境不会以任何方式改变PYTHONPATH 变量。 这在该路径包括对虚拟环境所使用的 Python '
                      '版本不兼容的代码的引用时可能导致非预期的结果。 最佳实践是执行 bash 命令 unset PYTHONPATH 或你所使用的 shell '
                      '的等价命令。\n'
                      '\n'
                      '要撤销激活一个虚拟环境，请输入:\n'
                      'deactivate\n'
                      '\n'
                      '到终端。\n'
                      '\n'
                      '## 12.3. 使用pip管理包¶\n'
                      '\n'
                      '你可以使用一个名为 pip 的程序来安装、升级和移除软件包。 默认情况下pip 将从 Python Package Index 安装软件包。 '
                      '你可以在你的 web 浏览器中查看 Python Package Index。\n'
                      '\n'
                      'pip 有许多子命令: "install", "uninstall", "freeze" 等等。 （请在 安装 Python 模块 指南页查看完整的 '
                      'pip 文档。）\n'
                      '\n'
                      '您可以通过指定包的名称来安装最新版本的包：\n'
                      '(tutorial-env) $ python -m pip install novas\n'
                      'Collecting novas\n'
                      '  Downloading novas-3.1.1.3.tar.gz (136kB)\n'
                      'Installing collected packages: novas\n'
                      '  Running setup.py install for novas\n'
                      'Successfully installed novas-3.1.1.3\n'
                      '\n'
                      '您还可以通过提供包名称后跟== 和版本号来安装特定版本的包：\n'
                      '(tutorial-env) $ python -m pip ins',
              'title': '12. 虚拟环境和包',
              'authors': [],
              'provider': 'deepseek',
              'source_kind': 'web',
              'content_scope': 'search_excerpt',
              'excerpt_truncated': True},
             {'id': 'S11',
              'doi': '',
              'url': 'https://docs.python.org/zh-cn/3.14/library/venv.html',
              'date': '',
              'text': '# venv --- 虚拟环境的创建¶\n'
                      '\n'
                      '在 3.3 版本加入.\n'
                      '\n'
                      '源码： Lib/venv/\n'
                      '\n'
                      'venv 模块支持创建轻量的“虚拟环境”，每个虚拟环境将拥有它们自己独立的安装在其 site 目录中的 Python 软件包集合。 虚拟环境是在现有的 '
                      'Python '
                      '安装版基础之上创建的，这被称为虚拟环境的“基础”Python，并且默认与基础环境中的软件包隔离开来，这样只有在虚拟环境中显式安装的软件包才是可用的。 '
                      '有关更多信息，请参阅 虚拟环境 和 site 的 虚拟环境文档。\n'
                      '\n'
                      '当在虚拟环境中使用时，常见安装工具如 pip 将把 Python 软件包安装到虚拟环境而无需显式地指明这一点。\n'
                      '\n'
                      '虚拟环境是（主要的特性）：\n'
                      '\n'
                      '用来包含支持一个项目（库或应用程序）所需的特定 Python 解释器、软件库和二进制文件。 它们在默认情况下与其他虚拟环境中的软件以及操作系统中安装的 '
                      'Python 解释器和库保持隔离。\n'
                      '\n'
                      '包含在一个目录中，根据惯例被命名为项目目录下的\n'
                      '.venv 或 venv，或是有许多虚拟环境的容器目录下，如 ~/.virtualenvs。\n'
                      '\n'
                      '不可签入 Git 等源代码控制系统。\n'
                      '\n'
                      '被认为是可丢弃的 -- 它应当能被简单地删除并从头开始重建。 你不应在此环境中放置任何项目代码。\n'
                      '\n'
                      '不被视为是可移动或可复制的 —— 你只能在目标位置重建相同的环境。\n'
                      '\n'
                      '请参阅 PEP 405 了解有关 Python 虚拟环境的更多背景信息。\n'
                      '\n'
                      '可用性: not Android, not iOS, not WASI.\n'
                      '\n'
                      '此模块在 移动平台 或 WebAssembly 平台 上不受支持。\n'
                      '\n'
                      '## 创建虚拟环境¶\n'
                      '\n'
                      '虚拟环境 是通过执行venv 模块来创建的：\n'
                      'python -m venv /path/to/new/virtual/environment\n'
                      '\n'
                      '此命令会创建目标目录（必要时还包括父目录）并在其中放置一个pyvenv.cfg 文件，文件带有一个指向运行此命令的 Python 安装版的 home '
                      '键。 它还会创建一个 bin 子目录 (在 Windows 上为 Scripts)，其中包含 Python 可执行文件的副本或符号链接 '
                      '(具体由创建环境时所使用的平台或参数而定)。 它还会创建一个 lib/pythonX.Y/site-packages 子目录 (在 Windows '
                      '上则为 Lib\\site-packages )。 如果指定了现有的目录，则该目录将被重用。\n'
                      '\n'
                      '在 3.5 版本发生变更: 现在推荐使用venv 来创建虚拟环境。\n'
                      '\n'
                      '从 3.6 版起已弃用，已在 3.8 版中移除: pyvenv 是针对 Python 3.3 和 3.4 创建虚拟环境的推荐工具，并在 3.5 '
                      '中被直接执行venv 的方式所取代。\n'
                      '\n'
                      '在 Windows 上，像下面这样唤起venv 命令：\n'
                      'PS> python -m venv C:\\path\\to\\new\\virtual\\environment\n'
                      '\n'
                      '本命令如果以-h 参数运行，将显示可用的选项:\n'
                      'usage: venv [-h] [--system-site-packages] [--symlinks | --copies] '
                      '[--clear]\n'
                      '            [--upgrade] [--without-pip] [--prompt PROMPT] [--upgrade-deps]\n'
                      '            [--without-scm-ignore-files]\n'
                      '            ENV_DIR [ENV_DIR ...]\n'
                      '\n'
                      'Creates virtual Python environments in one or more target directories.\n'
                      '\n'
                      'Once an environment has been created, you may wish to activate it, e.g. by\n'
                      'sourcing an activate script in its bin directory.\n'
                      '\n'
                      '- ENV_DIR¶\n'
                      '\n'
                      '一个必需参数，用于指定创建环境所在的目录。\n'
                      '\n'
                      '- --system-site-packages¶\n'
                      '\n'
                      '授予虚拟环境访问系统 site-packages 目录的权限。\n'
                      '\n'
                      '- --symlinks¶\n'
                      '\n'
                      '当符号链接不是平台的默认选项时，尽量使用符号链接而非复制操作。\n'
                      '\n'
                      '- --copies¶\n'
                      '\n'
                      '即使符号链接是平台的默认选项，也尽量使用复制操作而非符号链接。\n'
                      '\n'
                      '- --clear¶\n'
                      '\n'
                      '在创建环境之前，如果环境目录已存在，则删除其内容。\n'
                      '\n'
                      '- --upgrade¶\n'
                      '\n'
                      '将环境目录升级为使用此版本的 Python，前提是 Python 已就地升级。\n'
                      '\n'
                      '- --without-pi',
              'title': 'venv --- 虚拟环境的创建',
              'authors': [],
              'provider': 'deepseek',
              'source_kind': 'web',
              'content_scope': 'search_excerpt',
              'excerpt_truncated': True}]}


if __name__ == '__main__':
    unittest.main()
