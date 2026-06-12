# Skill: Search Chat History Across Sessions

**Procedure:**
1. Call `search_chat_history(query="<keywords>")` with a relevant search term and limit=20 (max)
2. For each result, extract chat_id or chat_name from the response
3. Use `get_chat_session(chat_id=<id>)` to load full message logs of specific sessions if needed
4. Compare dates/times between current session and retrieved histories to verify persistence

**Pitfalls:**
- Empty results may indicate recent changes to search indexing or query being too broad/narrow
- Session IDs change after new chats are created (can't reference by ID from old session)
- Large sessions (>20 messages) require multiple `get_chat_session()` calls for full content
- Cross-session queries work but don't preserve context—each session is isolated conversation state

**Verification:**
- Results should show timestamps matching current date/time patterns (e.g., "May 27, 2026")
- Check that retrieved sessions contain the keywords searched for in their messages
- Verify message counts match what was displayed in search results list
- Confirm persistence by comparing session summaries across multiple retrieval calls