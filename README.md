# ontoMeta

建立在 DataHub 之上的企业级本体建模系统，实现「读取元数据 → 生成草稿 → 工作区编辑确认 → 发布本体」的完整闭环。

## 项目结构

```
ontoMeta/
├── backend/          # FastAPI 后端
│   └── app/
│       ├── api/              # REST API 路由
│       ├── connectors/       # DataHub 接入层
│       ├── models/           # SQLAlchemy 数据模型
│       ├── schemas/          # Pydantic 请求/响应模型
│       └── services/         # 核心业务服务
├── frontend/         # React 前端
└── *.md              # 产品与技术文档
```

## 快速开始

### 后端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

默认启用 Mock 模式（`USE_MOCK_DATAHUB=true`、`USE_MOCK_LLM=true`），无需真实 DataHub 或 OpenAI 即可体验完整流程。

API 文档：http://localhost:8000/docs

### 前端

```bash
cd frontend
npm install
npm run dev
```

访问 http://localhost:5173

## 核心能力（第一阶段）

| 模块 | 说明 |
|------|------|
| DataHub Connector | 读取数据域、数据集、字段、血缘、逻辑证据 |
| Evidence Builder | 整理 LLM 证据包 |
| Draft Generator | 分步生成本体草稿（对象、属性、关系、业务逻辑） |
| Confirmation Service | 发布/删除等重要操作二次确认 |
| Publish Service | 草稿发布与版本记录 |
| Query Service | 本体、对象、业务逻辑只读查询 |

## 前端导航

- **工作区**：按数据域组织建模任务，触发草稿生成与发布
- **本体**：业务对象与关系的只读展示（列表/卡片/图谱）
- **业务逻辑**：指标、标签、规则的只读展示

## 配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `USE_MOCK_DATAHUB` | 使用 Mock 数据 | `false` |
| `USE_MOCK_LLM` | 使用规则引擎代替 LLM | `true` |
| `DATAHUB_GMS_URL` | DataHub GMS API 地址（GraphQL） | `http://localhost:8080` |
| `DATAHUB_FRONTEND_URL` | DataHub 前端地址（页面链接） | `http://localhost:9002` |
| `OPENAI_API_KEY` | OpenAI API Key | - |
| `DATABASE_URL` | 数据库连接 | `sqlite:///./ontometa.db` |

## 文档

- [SSOT.md](./SSOT.md) — 项目 Single Source of Truth
- [PRD.md](./PRD.md) — 产品需求
- [TECH_DESIGN.md](./TECH_DESIGN.md) — 技术设计
- [DOMAIN_MODEL.md](./DOMAIN_MODEL.md) — 领域模型
- [IA.md](./IA.md) — 信息架构
