# IPO Copilot & Cashflow Scheduler

This repository contains the **IPO Copilot & Cashflow Scheduler**, a tool designed to help you decide which IPOs to bid on, from which PAN account, and on which day, maximizing grey-market premium returns.

## Project Structure

The main application source code is located in the `ipo api/` directory.

- `ipo api/backend/`: FastAPI backend with an algorithmic scheduler, built with Python and `uv`.
- `ipo api/frontend/`: Next.js web application for the UI dashboard and chat copilot.

## Quick Start (Installation & Running)

The application consists of two processes. No database server or Docker is required for local development.

### 1. Start the Backend

Make sure you have [uv](https://github.com/astral-sh/uv) installed, then run:

```bash
cd "ipo api/backend"
uv sync --group dev
uv run uvicorn app.main:app --reload --port 8000
```

### 2. Start the Frontend

In a new terminal window, make sure you have Node.js and npm installed, then run:

```bash
cd "ipo api/frontend"
npm install
npm run dev
```

Then open <http://localhost:3000> in your browser to view the dashboard. The database will seed itself on the first boot.

### Enabling the AI Copilot (Optional)

To enable the Anthropic AI copilot in the frontend chat drawer, you'll need to set up your API key. Create a `.env.local` file in the `ipo api/frontend/` folder:

```bash
ANTHROPIC_API_KEY=your-anthropic-api-key-here
```

---

For more detailed technical documentation, design decisions, and testing notes, see the full documentation inside the [`ipo api/README.md`](ipo%20api/README.md) file.

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.
