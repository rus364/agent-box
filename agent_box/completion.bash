# agent-box bash completion; load with: source <(agent-box completion bash)
# Candidates come from `agent-box _complete`, so this script never lists commands itself.
# The agent-box that printed this script answers, so an alias spelling its path in any way completes alike.

# Bash picks a completion by the typed word and never expands an alias for it, so the
# caller's `words` has an alias in words[0] replaced by its expansion as bash would: nested
# ones too, but none twice, so `alias agent-box='agent-box --review off'` ends its chain.
# Success means the words now start with agent-box or a path to it, whose arguments follow.
_agent_box_unalias() {
    local text seen=" "
    local -a expansion
    while [[ -n ${words[0]} && -n ${BASH_ALIASES[${words[0]}]+set} && $seen != *" ${words[0]} "* ]]; do
        seen+="${words[0]} "
        text=${BASH_ALIASES[${words[0]}]}
        if [[ $text == *[\"\'\\]* ]]; then
            # xargs honours quotes without expanding anything; eval would run the alias's substitutions on Tab.
            mapfile -td '' expansion < <(xargs printf '%s\0' <<< "$text" 2>/dev/null)
        else
            read -ra expansion <<< "$text"
        fi
        ((${#expansion[@]})) || return 1
        words=("${expansion[@]}" "${words[@]:1}")
    done
    [[ ${words[0]} == agent-box || ${words[0]} == */agent-box ]]
}

_agent_box() {
    local line=${COMP_LINE:0:COMP_POINT} mode word cur drop
    local -a words typed reply
    read -ra words <<< "$line"
    [[ -z $line || $line == *[[:space:]] ]] && words+=("")
    # A wrapper function or symlink registered by hand passes its arguments unchanged.
    typed=("${words[@]}")
    _agent_box_unalias || words=("${typed[@]}")
    # Readline replaces only the text between the last word break (= or :) and the cursor;
    # COMP_WORDS[COMP_CWORD] is not that text when the cursor is mid-word or right after a break.
    cur=${words[-1]##*[$COMP_WORDBREAKS]}
    { IFS= read -r mode; mapfile -t reply; } < <(command @AGENT_BOX@ _complete "${words[@]:1}" 2>/dev/null)
    COMPREPLY=()
    case $mode in
        files) compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -f -- "$cur") ;;
        dirs) compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -d -- "$cur") ;;
        *)
            # Candidates are whole words, so they lose what precedes the replaced text.
            drop=$(( ${#words[-1]} - ${#cur} ))
            for word in "${reply[@]}"; do
                word=${word%%$'\t'*}
                word=${word:drop}
                # A lifecycle state such as In progress is one argument; readline inserts it verbatim.
                [[ $word == *[[:space:]]* ]] && printf -v word '%q' "$word"
                COMPREPLY+=("$word")
            done
            ;;
    esac
}

complete -F _agent_box agent-box
# Aliases defined before this line complete like agent-box; a later one has to be registered by hand.
_agent_box_register() {
    local name
    local -a words
    for name in "${!BASH_ALIASES[@]}"; do
        words=("$name")
        if _agent_box_unalias; then complete -F _agent_box "$name"; fi
    done
}
_agent_box_register
unset -f _agent_box_register
