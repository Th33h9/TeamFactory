硬性过滤条件
git clone 必须成功。
必须是 Python 项目，核心实现语言为 Python。
必须能在目标环境中 build / install 成功。
必须包含 pytest 测试文件，test_files 非空。
test_case_count > 0。
dryrun / oracle 必须能跑出结构化结果：collected / passed / failed / errors。
oracle 必须能在 --network none 下运行，证明不依赖联网、API key 或外部服务。
不依赖 GUI、浏览器、数据库服务、MinIO、Redis、Elasticsearch、Docker-in-Docker、后端常驻服务等外部运行组件。
repo name 不能出现在 nl2repobench 原始仓库列表中。
repo 不能和已有 candidate jsonl 中的 repo 重复。
代码仓库总 Python LOC 必须在 2000-8000 LOC。
需要 agent 实现的 .py 文件数必须 >= 5。
项目规模必须适合 0-to-1 重建：不能是大型框架、完整 Web 产品、复杂分布式系统、深度依赖平台工程或高度专业化巨型项目。
类型要求
候选 repo 应接近 nl2repobench 的任务形态，优先选择以下类型：
纯 Python library
parser / serializer / converter
validator / checker / linter-like tool
utility library
pytest plugin / testing utility
小型 CLI + library 混合包
data processing / format processing 工具
lightweight ML / data analysis utility
networking protocol/parser/tool library
system/file/batch processing utility
类别覆盖要求
最终集合必须覆盖以下 9 类，每类都要有一定数量，不能全部集中在 parser/utility：
Web Development
Testing
Utility Libraries
Machine Learning
Data Analysis & Processing
Database Interaction
Networking Tools
Batch File Processing
System Tools
排除规则
以下 repo 即使满足 pytest，也应过滤：
需要真实网络请求、云服务、OAuth、API key。
需要 GUI、浏览器、Playwright/Selenium、桌面环境。
需要外部数据库、消息队列、对象存储或后台服务。
测试主要是 integration/e2e，而不是本地 unit pytest。
测试依赖当前时间、随机外部状态或大型下载资源。
项目只有薄 wrapper，核心逻辑依赖远端服务。
项目太小，agent 只需实现少量文件或 API。
项目太大，超过 8000 LOC 或依赖/架构复杂度明显不适合从零重建。
测试大量 mock 私有服务、私有路径、私有数据。
license 或仓库状态不适合复用。
最终验收标准
一个 repo 只有在同时满足以下条件时才进入最终 url.jsonl：
clone_ok == true
is_python_project == true
install_ok == true
test_files_count > 0
test_case_count > 0
oracle_has_structured_result == true
oracle_network_none_ok == true
requires_external_service == false
python_loc >= 2000
python_loc <= 8000
agent_target_py_files >= 5
not_in_nl2repobench == true
not_seen_before == true
category in {
  web_development,
  testing,
  utility_libraries,
  machine_learning,
  data_analysis_processing,
  database_interaction,
  networking_tools,
  batch_file_processing,
  system_tools
}