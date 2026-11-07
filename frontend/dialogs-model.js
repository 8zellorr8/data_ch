export function summarizeEvaluation(result) {
  const answers = result.final?.answers || [];
  return {
    id: result.id, createdAt: result.created_at, origin: result.origin,
    score: typeof result.score?.total === 'number' ? result.score.total : null,
    checklistId: result.checklist?.checklist_id, checklistName: result.checklist?.name || 'Чек-лист', checklistVersion: result.checklist?.version,
    needsReview: answers.some(answer => answer.needs_review),
    hasViolation: (result.score?.flags || []).some(id => answers.some(answer => answer.criterion_id === id && !answer.needs_review && answer.value === 'yes')),
  };
}

export function datasetRows(snapshot) {
  const latest = new Map();
  for (const result of snapshot.evaluations || []) {
    const old = latest.get(result.conversation_id);
    if (!old || result.created_at > old.created_at || (result.created_at === old.created_at && result.id > old.id)) latest.set(result.conversation_id, result);
  }
  return snapshot.conversations.map(({transcript, ...conversation}) => ({
    ...conversation, message_count: transcript?.length ?? conversation.message_count ?? 0,
    evaluation: latest.has(conversation.id) ? summarizeEvaluation(latest.get(conversation.id)) : null,
  }));
}

export function filterRows(rows, {search = '', language = '', channel = '', status = '', sort = ''} = {}) {
  const query = search.trim().toLocaleLowerCase();
  const result = rows.filter(row =>
    (!query || row.conversation_id.toLocaleLowerCase().includes(query)) &&
    (!language || row.language === language) && (!channel || row.channel === channel) &&
    (!status || (status === 'unrated' && !row.evaluation) ||
      (status === 'review' && row.evaluation?.needsReview) ||
      (status === 'violation' && row.evaluation?.hasViolation)));
  if (sort === 'asc' || sort === 'desc') result.sort((a, b) => {
    const left = a.evaluation?.score, right = b.evaluation?.score;
    if (left == null && right == null) return a.conversation_id.localeCompare(b.conversation_id);
    if (left == null) return 1;
    if (right == null) return -1;
    return (sort === 'asc' ? left - right : right - left) || a.conversation_id.localeCompare(b.conversation_id);
  });
  return result;
}
