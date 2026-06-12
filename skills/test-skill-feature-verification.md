# Skill: Test Skill: Feature Verification

## Procedure
1. Create a Python script with the required function signatures (tool function + register_{name}_tool(registry))
2. Execute `create_tool` or write to disk and hot-load
3. Verify the skill appears in `list_skills`
4. Confirm retrieval works via `recall_skill`

## Pitfalls
- Don't forget the required `register_{name}_tool(registry)` function at module level
- The content must be valid Markdown with headers (#, ##)
- Ensure no syntax errors before writing to disk

## Verification
Run `list_skills` and confirm "Test Skill: Feature Verification" appears in the output.