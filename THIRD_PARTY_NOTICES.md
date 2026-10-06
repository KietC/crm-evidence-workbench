# Third-party notices

## English

Our source is MIT-licensed. Dependencies and external tools keep their own licenses; this license does not relicense them. No third-party model weights, browser binary, Office installation, library source or captured dataset is vendored here. npm and Python install dependencies separately.

| Component | Purpose | Upstream license / reference |
| --- | --- | --- |
| ExcelJS | XLSX authoring and reading | [MIT](https://github.com/exceljs/exceljs/blob/master/LICENSE) |
| JSZip | OOXML ZIP inspection | [MIT option](https://github.com/Stuk/jszip/blob/main/LICENSE.markdown) |
| Sharp / libvips | Local PNG layout previews | [Apache-2.0; bundled libraries have separate notices](https://github.com/lovell/sharp/blob/main/LICENSE) |
| Playwright Core | Browser automation protocol | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE) |
| Electron | Optional local browser application | [MIT and bundled component notices](https://github.com/electron/electron/blob/main/LICENSE) |
| TypeScript | Type checking and compilation | [Apache-2.0](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt) |
| markdownlint | Public-guide formatting checks | [MIT](https://github.com/DavidAnson/markdownlint/blob/main/LICENSE) |
| pdfplumber / pypdf | PDF text and structure | [pdfplumber MIT](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt), [pypdf BSD-3-Clause](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| Pillow / python-docx | Images and DOCX | [Pillow HPND](https://github.com/python-pillow/Pillow/blob/main/LICENSE), [python-docx MIT](https://github.com/python-openxml/python-docx/blob/master/LICENSE) |
| DuckDB | Local Parquet export | [MIT](https://github.com/duckdb/duckdb/blob/main/LICENSE) |
| Tesseract, Poppler, FFmpeg, 7-Zip, Ghostscript, LibreOffice | Optional local processing | Each upstream distribution's license applies; install independently |
| Microsoft Office COM | Optional Windows-native conversion and QA | Requires a separate licensed installation; not redistributed |
| Local ASR models/adapters | Optional media transcription | Separate adapter/model license; not included |

README structure was informed by [Best-README-Template](https://github.com/othneildrew/Best-README-Template) and [FastAPI](https://github.com/fastapi/fastapi). Their text and code were not copied into this release.

## 中文

本项目源码采用 MIT；依赖和外部工具保持自己的许可证，本许可证不重新授权它们。仓库不内置第三方模型权重、浏览器二进制、Office 安装、库源码或采集数据。npm 与 Python 分别安装依赖。

上表列出工作簿、图像、浏览器、编译、PDF、Word 和本地数据库依赖的用途及许可证来源。OCR、媒体、压缩包、转换等工具需单独安装并遵守各自许可证。Microsoft Office COM 仅是可选功能，需要已授权安装，不随仓库分发。ASR 适配器和模型也不内置，须核对各自授权。

README 结构参考 [Best-README-Template](https://github.com/othneildrew/Best-README-Template) 和 [FastAPI](https://github.com/fastapi/fastapi)，未复制它们的正文或代码。
