[**English**](./README.md) | [**简体中文**](./README.zh-CN.md)

# Hiyori Agent

An intelligent desktop pet based on LLM and Agent technologies. It includes a powerful backend based on **FastAPI + LangChain/LangGraph**, and a desktop UI component based on **Electron + Vite + Vue/React/TS** (supporting Live2D).

## Project Structure

The project adopts a decoupled architecture separating frontend and backend:

```text
hiyori_agent/
├── app/                  # FastAPI backend code
│   ├── agent/            # LangGraph Agent
│   ├── config/           # Backend configuration parsing
│   ├── crud/             # Database CRUD layer
│   ├── routes/           # API routing layer
│   ├── schemas/          # Pydantic data models
│   └── services/         # Service layer
├── config/               # Backend configuration files
├── memory/               # Memory storage
├── ui/                   # Frontend source code
│   ├── electron/         # Electron main process and preload scripts
│   ├── public/           # Static resources, Live2D model files, etc.
│   ├── src/              # Frontend page code
│   └── package.json      # Frontend dependencies and script configuration
├── tests/                # Backend automated tests
├── main.py               # FastAPI backend service entry point
└── requirements.txt      # Python environment dependency list
```

## Deployment and Execution

To run this project fully, you need to start the backend API service and the frontend client separately.

### 1. Backend Deployment (FastAPI)

The backend depends on the Python environment. Python 3.12 is recommended.

1. **Create and activate a virtual environment (miniconda is Recommended)**:
   ```powershell
   conda create -n hiyori_agent python=3.12
   conda activate hiyori_agent
   ```
2. **Install dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```
3. **Environment Configuration**:
   Create or update the `.env` file in the project root and fill in your model/search credentials:
   ```dotenv
   EMBEDDING_API_KEY=your_embedding_api_key
   EMBEDDING_MODEL=text-embedding-v4
   EMBEDDING_DIMENSION=1024
   EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

   CODING_API_KEY=your_coding_api_key
   CODING_MODEL=gpt-5.3-codex
   CODING_BASE_URL=https://www.dmxapi.cn/v1
   CODING_TEMPERATURE=0.3

   TAVILY_API_KEY=your_tavily_api_key
   ```
   > `config/settings.yaml` is no longer used at runtime (it is kept only as a migration hint file).
4. **Start the service**:
   ```powershell
   uvicorn main:app --reload --reload-exclude "agent_workspace/*"
   ```
   > The backend will run at http://127.0.0.1:8000 by default.

### 2. Frontend Deployment (Electron + Vite)

Frontend environment: Node.js 24.9.0

1. **Enter the frontend directory**:
   ```powershell
   cd ui
   ```
2. **Install dependencies**:
   ```powershell
   npm install
   ```
3. **Download Live2D model**
   Go to `https://cubism.live2d.com/sample-data/bin/hiyori_pro/hiyori_pro_zh.zip` to download.
   
   Place the downloaded zip file in the `/ui` directory.
4. **Run in development mode**:
   ```powershell
   npm run dev
   ```
   > Starts the Vite development server and opens the Electron client window, displaying the Live2D desktop pet and interacting with the backend.

## Notes

* **Database**: This project uses local SQLite for temporary storage of dialogue history and running status. The database files will be automatically generated in the `memory/sqlite/` directory.
* **Live2D Models**: Model assets are placed in the `ui/public/live2d/` directory. You can replace or add new ones in the frontend settings panel according to your needs.