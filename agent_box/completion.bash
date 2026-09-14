# agent-box bash completion; load with: source <(agent-box completion bash)
# Candidates come from `agent-box _complete`, so this script never lists commands itself.
_agent_box() {
    local cur=${COMP_WORDS[COMP_CWORD]} line=${COMP_LINE:0:COMP_POINT} mode word drop
    local -a words reply
    read -ra words <<< "$line"
    [[ -z $line || $line == *[[:space:]] ]] && words+=("")
    { IFS= read -r mode; mapfile -t reply; } < <(command agent-box _complete "${words[@]:1}" 2>/dev/null)
    COMPREPLY=()
    case $mode in
        files) compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -f -- "$cur") ;;
        dirs) compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -d -- "$cur") ;;
        *)
            # Candidates are whole words; bash replaces only what follows its last word break (= or :).
            drop=$(( ${#words[-1]} - ${#cur} ))
            for word in "${reply[@]}"; do
                word=${word%%$'\t'*}
                COMPREPLY+=("${word:drop}")
            done
            ;;
    esac
}
complete -F _agent_box agent-box
