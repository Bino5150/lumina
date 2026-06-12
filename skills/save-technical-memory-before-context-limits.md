# Skill: Save Technical Memory Before Context Limits

## Procedure

1. **Identify the content** - Determine what information needs preservation (article analysis, configuration details, architectural decisions, etc.)

2. **Select appropriate label** - Choose from existing categories or create new ones:
   - `technical-specs` for architecture and specs  
   - `projects` for ongoing development work  
   - `sessions` for temporary context  
   - `preferences` for user settings

3. **Compress content** - Use the memory compression feature to reduce token usage while preserving meaning:
   ```python
   save_memory(
       content=compressed_text,  # Use built-in compression
       label='technical-specs'
   )
   ```

4. **Verify retention** - Confirm via `get_recent_memories` or `palace_recall` that data persisted

5. **Document findings** - Save detailed analysis to knowledge base if needed for future reference

## Pitfalls
- Don't save entire conversation history (wastes tokens on conversational filler)
- Avoid redundant saves when content already exists in memory
- Compress before saving large codebases or lengthy articles
- Remember that memory is compressed but not deleted until session end (unless using decay algorithms)

## Verification
Check via:
- `get_recent_memories(limit=5)` - Shows most recent entries by label  
- `palace_recall('query')` - Full-text search in deep memories  
- `search_memory('keyword')` - Quick verification for specific content
- Compare before/after token counts using palace status
