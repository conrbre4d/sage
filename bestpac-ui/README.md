# Bestpac Banking UI

This folder contains the Bestpac banking assistant UI and personal-loan form.

## Run locally

1. Create a `.env` file from `.env.example` and set `API_KEY` to your Azure OpenAI API key.
2. Install the app dependencies:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

3. Start the server from this folder:

   ```bash
   python3 main.py
   ```

4. Open <http://127.0.0.1:8000>.

The form currently completes in the browser only. It does not save or submit application data, and selected documents are not uploaded.
