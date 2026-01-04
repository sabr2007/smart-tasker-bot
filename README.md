# Smart Tasker Bot

**Introduction**

I am a first-year student specializing in **Big Data Analysis**, and this is my first project. Originally designed as a personal tool to help organize my academic and daily life, it has evolved into a functional intelligent assistant.

**Project Overview**

Smart Tasker Bot is an AI-powered personal assistant integrated into Telegram. It leverages natural language processing to manage tasks, reminders, and schedules seamlessly. The goal is to minimize friction in task management — simply tell the bot what to do, and it handles the rest.

## Key Features

*   **Natural Language Processing**
    Add tasks using everyday speech (e.g., "Submit lab report next Tuesday at 10 AM"). The bot understands context and relative dates.

*   **Voice Command Support**
    Send voice notes which are automatically transcribed and processed into actionable items.

*   **Smart Scheduling**
    Supports one-time tasks, recurring events (daily, weekly, monthly), and complex date parsing.

*   **Interactive WebApp**
    A built-in Telegram Mini App provides a visual dashboard for managing your task list efficiently.

*   **Reliable Reminders**
    Automated notifications with interactive actions (Mark Done, Snooze for 5m/30m/1h) to ensure nothing falls through the cracks.

*   **Daily Digest**
    A morning summary of your upcoming tasks delivered at a scheduled time.

*   **Neural Inbox**
    Send screenshots, photos of announcements, or PDF documents. The bot uses **GPT-4o Vision** to analyze visual content, extract dates and tasks, and automatically populate your schedule.

*   **Smart Attachments**
    Attach files (files, tickets, scans) to specific tasks. The bot can retrieve them on demand ("Send me the ticket for the flight") or automatically send them along with the reminder.

## Technical Architecture

This project is built as a hybrid system, combining a traditional event-driven Telegram bot with an autonomous AI agent layer.

### Technology Stack

*   **Core**: Python 3.11+
*   **Framework**: `python-telegram-bot` (Asynchronous)
*   **Web Backend**: FastAPI
*   **Database**: PostgreSQL
*   **AI Engine**: OpenAI API (GPT-4o for multimodal analysis, intent parsing, and entity extraction)

### How It Works

1.  **Input Analysis**
    User inputs (text or voice) are intercepted by the **LLM Agent**. The agent analyzes the intent—whether it's creating a new task, modifying an existing one, or querying the schedule.

2.  **Entity Extraction**
    The system extracts structured data such as dates, specific times, and recurrence patterns from unstructured natural language inputs, converting phrases like "next Friday" into concrete ISO timestamps.

3.  **Data Management**
    All tasks are stored in a **PostgreSQL** database, ensuring data integrity, scalability, and persistence.

4.  **Event Loop & Scheduling**
    Background workers (utilizing `JobQueue`) monitor the database for impending deadlines, triggering push notifications independently of user interaction.

## Getting Started

Follow these steps to deploy the bot locally.

1.  **Clone the Repository**
    ```bash
    git clone <repository-url>
    ```

2.  **Environment Configuration**
    Create a `.env` file in the root directory with the following variables:
    ```env
    TELEGRAM_BOT_TOKEN=your_token_here
    OPENAI_API_KEY=your_openai_key
    DATABASE_URL=postgresql://user:pass@localhost/dbname
    ```

3.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Launch**
    ```bash
    python src/main.py
    ```

---
*Developed by a Big Data Analysis student.*
