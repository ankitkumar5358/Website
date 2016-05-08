# encoding=utf-8

from flask import (
    redirect, url_for, request, abort, render_template,
    flash, Blueprint, session, current_app as app
)
from flask.ext.login import current_user

from sqlalchemy import func, or_
from sqlalchemy.orm import aliased

from wtforms import (
    SubmitField, StringField, FieldList, FormField, SelectField, TextAreaField,
    BooleanField, IntegerField, FloatField
)
from wtforms.validators import Required, NumberRange, ValidationError

import random
from datetime import datetime, timedelta
from main import db, external_url
from .common import require_permission, send_template_email
from .majority_judgement import calculate_max_normalised_score

from models.cfp import (
    Proposal, CFPMessage, CFPVote, CFP_STATES
)
from .common.forms import Form, HiddenIntegerField

cfp_review = Blueprint('cfp_review', __name__)
admin_required = require_permission('admin')  # Decorator to require admin permissions
anon_required = require_permission('cfp_anonymiser')
review_required = require_permission('cfp_reviewer')
ordered_states = ['edit', 'new', 'locked', 'checked', 'rejected', 'anonymised',
                  'anon-blocked', 'reviewed', 'accepted', 'finished']

@cfp_review.context_processor
def cfp_review_variables():
    unread_count = CFPMessage.query.filter(
        # is_to_admin AND (has_been_read IS null OR has_been_read IS false)
        or_(CFPMessage.has_been_read.is_(False),
            CFPMessage.has_been_read.is_(None)),
        CFPMessage.is_to_admin.is_(True)
    ).count()

    count_dict = dict(Proposal.query.with_entities(
        Proposal.state,
        func.count(Proposal.state),
    ).group_by(Proposal.state).all())
    proposal_counts = {state: count_dict.get(state, 0) for state in CFP_STATES}

    unread_reviewer_notes = CFPVote.query.filter(
        or_(CFPVote.has_been_read.is_(False),
            CFPVote.has_been_read.is_(None))
    ).count()

    return {
        'ordered_states': ordered_states,
        'unread_count': unread_count,
        'proposal_counts': proposal_counts,
        'unread_reviewer_notes': unread_reviewer_notes,
        'view_name': request.url_rule.endpoint.replace('cfp_review.', '.')
    }

@cfp_review.route('')
def main():
    if current_user.is_anonymous():
        return redirect(url_for('users.login', next=url_for('.main')))

    if current_user.has_permission('admin'):
        return redirect(url_for('.proposals'))

    if current_user.has_permission('cfp_anonymiser'):
        return redirect(url_for('.anonymisation'))

    if current_user.has_permission('cfp_reviewer'):
        return redirect(url_for('.review_list'))

    abort(404)


def build_query_dict(parameters):
    res = {}
    fields = [('type', str), ('state', str), ('needs_help', bool), ('needs_money', bool)]

    for (field_name, field_type) in fields:
        # if this can't convert to the correct type it will return None
        val = parameters.get(field_name, None)

        if val is not None:
            try:
                val = field_type(val)
            except ValueError:
                flash('Invalid parameter value (%r) for parameter %s' % (val, field_name))
                continue
            res[field_name] = val

    return res


@cfp_review.route('/proposals')
@admin_required
def proposals():
    query_dict = build_query_dict(request.args)

    proposals = Proposal.query.filter_by(**query_dict)\
                              .order_by('state', 'modified', 'id').all()

    return render_template('cfp_review/proposals.html', proposals=proposals,
                           link_target='.update_proposal')


class UpdateProposalForm(Form):
    # Admin can change anything
    state = SelectField('State', choices=[(s, s) for s in ordered_states])
    title = StringField('Title', [Required()])
    description = TextAreaField('Description', [Required()])
    requirements = StringField('Requirements')
    length = StringField('Length')
    notice_required = SelectField("Required notice",
                                  choices=[('1 week', '1 week'),
                                           ('1 month', '1 month'),
                                           ('> 1 month', 'Longer than 1 month')])
    needs_help = BooleanField('Needs Help')
    needs_money = BooleanField('Needs Money')
    one_day = BooleanField('One day only')

    update = SubmitField('Force update')
    reject = SubmitField('Reject')
    checked = SubmitField('Send for Anonymisation')

    def update_proposal(self, proposal):
        proposal.title = self.title.data
        proposal.description = self.description.data
        proposal.requirements = self.requirements.data
        proposal.length = self.length.data
        proposal.notice_required = self.notice_required.data
        proposal.needs_help = self.needs_help.data
        proposal.needs_money = self.needs_money.data
        proposal.one_day = self.one_day.data

class UpdateWorkshopForm(UpdateProposalForm):
    cost = StringField('Cost')
    attendees = StringField('Attendees', [Required()])

    def update_proposal(self, proposal):
        proposal.cost = self.cost.data
        proposal.attendees = self.attendees.data
        super(UpdateWorkshopForm, self).update_proposal(proposal)


class UpdateInstallationForm(UpdateProposalForm):
    funds = StringField('Funds')
    size = StringField('Size', [Required()])

    def update_proposal(self, proposal):
        proposal.size = self.size.data
        proposal.funds = self.funds.data
        super(UpdateInstallationForm, self).update_proposal(proposal)


def get_next_proposal_to(prop, state):
    return Proposal.query.filter(
        Proposal.id != prop.id,
        Proposal.state == state,
        Proposal.modified >= prop.modified # ie find something after this one
    ).order_by('modified', 'id').first()

@cfp_review.route('/proposals/<int:proposal_id>', methods=['GET', 'POST'])
@admin_required
def update_proposal(proposal_id):
    prop = Proposal.query.get(proposal_id)
    next_prop = get_next_proposal_to(prop, prop.state)

    next_id = next_prop.id if next_prop else None

    form = UpdateProposalForm() if prop.type == 'talk' else \
           UpdateWorkshopForm() if prop.type == 'workshop' else \
           UpdateInstallationForm()

    # Process the POST
    if form.validate_on_submit():
        if form.update.data:
            app.logger.info('Updating proposal %s', proposal_id)
            form.update_proposal(prop)
            # NB bypasses the transition rules
            prop.state = form.state.data
            flash('Changes saved')

        elif form.reject.data:
            app.logger.info('Rejecting proposal %s', proposal_id)
            form.update_proposal(prop)
            prop.set_state('rejected')
            flash('Rejected')

        elif form.checked.data:
            app.logger.info('Sending proposal %s for anonymisation', proposal_id)
            form.update_proposal(prop)
            prop.set_state('checked')

            db.session.commit()
            if not next_id:
                return redirect(url_for('.proposals'))
            return redirect(url_for('.update_proposal', proposal_id=next_id))

        db.session.commit()
        return redirect(url_for('.update_proposal', proposal_id=proposal_id))

    form.state.data = prop.state
    form.title.data = prop.title
    form.description.data = prop.description
    form.requirements.data = prop.requirements
    form.length.data = prop.length
    form.notice_required.data = prop.notice_required
    form.needs_help.data = prop.needs_help
    form.needs_money.data = prop.needs_money
    form.one_day.data = prop.one_day

    if prop.type == 'workshop':
        form.attendees.data = prop.attendees
        form.cost.data = prop.cost

    elif prop.type == 'installation':
        form.size.data = prop.size
        form.funds.data = prop.funds

    return render_template('cfp_review/update_proposal.html',
                            proposal=prop, form=form, next_id=next_id)


@cfp_review.route('/messages')
@admin_required
def all_messages():
    # TODO add search
    # Query from the proposal because that's actually what we display
    proposal_with_message = Proposal.query\
        .join(CFPMessage)\
        .filter(Proposal.id == CFPMessage.proposal_id)\
        .order_by(CFPMessage.has_been_read, CFPMessage.created.desc())\
        .all()

    proposal_with_message.sort(key=lambda x: (x.get_unread_count(current_user) > 0,
                                              x.messages[-1].created), reverse=True)

    return render_template('cfp_review/all_messages.html',
                           proposal_with_message=proposal_with_message)


class SendMessageForm(Form):
    message = TextAreaField('New Message')
    send = SubmitField('Send Message')
    mark_read = SubmitField('Mark all as read')


@cfp_review.route('/proposals/<int:proposal_id>/message', methods=['GET', 'POST'])
@admin_required
def message_proposer(proposal_id):
    form = SendMessageForm()
    proposal = Proposal.query.get(proposal_id)

    if request.method == 'POST':
        if form.send.data and form.message.data:
            msg = CFPMessage()
            msg.is_to_admin = False
            msg.from_user_id = current_user.id
            msg.proposal_id = proposal_id
            msg.message = form.message.data

            db.session.add(msg)
            db.session.commit()

            app.logger.info('Sending message from %s to %s', current_user.id, proposal.user_id)

            msg_url = external_url('cfp.proposal_messages', proposal_id=proposal_id)
            send_template_email('New message about your EMF proposal',
                                proposal.user.email, app.config['CONTENT_EMAIL'],
                                'cfp_review/email/new_message.txt', url=msg_url,
                                to_user=proposal.user, from_user=current_user,
                                proposal=proposal)

        if form.mark_read.data or form.send.data:
            count = proposal.mark_messages_read(current_user)
            app.logger.info('Marked %d messages to admin on proposal %d as read' % (count, proposal.id))

        return redirect(url_for('.message_proposer', proposal_id=proposal_id))

    # Admin can see all messages sent in relation to a proposal
    messages = CFPMessage.query.filter_by(
        proposal_id=proposal_id
    ).order_by('created').all()

    return render_template('cfp_review/message_proposer.html',
                           form=form, messages=messages, proposal=proposal)

@cfp_review.route('/votes')
@admin_required
def vote_summary():
    proposals = Proposal.query.filter_by(state='anonymised')\
                              .order_by('modified').all()

    proposals_with_counts = []
    for prop in proposals:
        state_counts = {}
        for v in prop.votes:
            state_counts.setdefault(v.state, 0)
            state_counts[v.state] += 1
        proposals_with_counts.append((prop, state_counts))

    sort_key = lambda p: (p[0].get_unread_vote_note_count() > 0, p[0].created)
    proposals_with_counts.sort(key=sort_key, reverse=True)

    return render_template('cfp_review/vote_summary.html',
                            proposals_with_counts=proposals_with_counts)


class ResolveVoteForm(Form):
    id = HiddenIntegerField('Vote Id')
    resolve = BooleanField("Set to 'resolved'")


class UpdateVotesForm(Form):
    votes_to_resolve = FieldList(FormField(ResolveVoteForm))
    include_recused = BooleanField("Also set 'recused' votes to 'stale'")
    set_all_stale = SubmitField("Set all votes to 'stale'")
    resolve_all = SubmitField("Set all 'blocked' votes to 'resolved'")
    update = SubmitField("Set selected votes to 'resolved'")
    set_all_read = SubmitField("Set all notes to read")


@cfp_review.route('/proposals/<int:proposal_id>/votes', methods=['GET', 'POST'])
@admin_required
def proposal_votes(proposal_id):
    form = UpdateVotesForm()
    proposal = Proposal.query.get(proposal_id)
    all_votes = {v.id: v for v in proposal.votes}

    if form.validate_on_submit():
        msg = ''
        if form.set_all_stale.data:
            stale_count = 0
            states_to_set = ['voted', 'blocked', 'recused'] if form.include_recused.data\
                                                            else ['voted', 'blocked']
            for vote in all_votes.values():
                if vote.state in states_to_set:
                    vote.set_state('stale')
                    stale_count += 1

            if stale_count:
                msg = 'Set %d votes to stale' % stale_count

        elif form.update.data:
            update_count = 0
            for form_vote in form.votes_to_resolve:
                vote = all_votes[int(form_vote['id'].data)]
                if form_vote.resolve.data and vote.state in ['blocked', 'recused']:
                    vote.set_state('resolved')
                    update_count += 1

            if update_count:
                msg = 'Set %d votes to resolved' % update_count

        elif form.resolve_all.data:
            resolved_count = 0
            for vote in all_votes.values():
                if vote.state == 'blocked':
                    vote.set_state('resolved')
                    resolved_count += 1

        if msg:
            flash(msg)
            app.logger.info(msg)

        # Regardless, set everything to read
        for v in all_votes.values():
            v.has_been_read = True

        db.session.commit()
        return redirect(url_for('.proposal_votes', proposal_id=proposal_id))

    for v_id in all_votes:
        form.votes_to_resolve.append_entry()
        form.votes_to_resolve[-1]['id'].data = v_id

    return render_template('cfp_review/proposal_votes.html',
                           proposal=proposal, form=form, votes=all_votes)


@cfp_review.route('/anonymisation')
@anon_required
def anonymisation():
    proposals = Proposal.query.filter_by(state='checked').order_by('modified', 'id').all()

    return render_template('cfp_review/proposals.html', proposals=proposals,
                           link_target='.anonymise_proposal')


class AnonymiseProposalForm(Form):
    title = StringField('Title', [Required()])
    description = TextAreaField('Description', [Required()])
    anonymise = SubmitField('Send to review and go to next')
    reject = SubmitField('I cannot anonymise this proposal')


@cfp_review.route('/anonymisation/<int:proposal_id>', methods=['GET', 'POST'])
@anon_required
def anonymise_proposal(proposal_id):
    prop = Proposal.query.get(proposal_id)
    if prop.state != 'checked':
        # Make sure people only see proposals that are ready
        return abort(404)

    next_prop = get_next_proposal_to(prop, 'checked')
    form = AnonymiseProposalForm()

    if form.validate_on_submit():
        if form.reject.data:
            prop.set_state('anon-blocked')
            prop.anonymiser_id = current_user.id
            db.session.commit()
            app.logger.info('Proposal %s cannot be anonymised', proposal_id)

        if form.anonymise.data:
            prop.title = form.title.data
            prop.description = form.description.data
            prop.set_state('anonymised')
            prop.anonymiser_id = current_user.id
            db.session.commit()
            app.logger.info('Sending proposal %s for review', proposal_id)

        if not next_prop:
            return redirect(url_for('.anonymisation'))
        return redirect(url_for('.anonymise_proposal', proposal_id=next_prop.id))


    form.title.data = prop.title
    form.description.data = prop.description

    return render_template('cfp_review/anonymise_proposal.html',
                           proposal=prop, form=form, next_proposal=next_prop)


class ReviewListForm(Form):
    show_proposals = SubmitField("Show me some more proposals")
    reload_proposals = SubmitField("Show some different proposals")

@cfp_review.route('/review', methods=['GET', 'POST'])
@review_required
def review_list():
    form = ReviewListForm()

    if form.validate_on_submit():
        app.logger.info('Clearing review order')
        session['review_order'] = None
        session['review_order_dt'] = datetime.utcnow()
        return redirect(url_for('.review_list'))

    review_order_dt = session.get('review_order_dt')

    last_visit = session.get('review_visit_dt')
    if not last_visit:
        last_vote_cast = CFPVote.query.filter_by(user_id=current_user.id) \
            .order_by(CFPVote.modified.desc()).first()

        if last_vote_cast:
            last_visit = last_vote_cast.modified
            review_order_dt = last_vote_cast.modified

    proposal_query = Proposal.query.filter(Proposal.state == 'anonymised')

    if not current_user.has_permission('admin'):
        # reviewers shouldn't see their own proposals, and don't review installations
        proposal_query = proposal_query.filter(
            Proposal.user_id != current_user.id,
            Proposal.type.in_(['talk', 'workshop']))

    to_review_again = []
    to_review_new = []
    to_review_old = []
    reviewed = []

    user_votes = aliased(CFPVote, CFPVote.query.filter_by(user_id=current_user.id).subquery())

    for proposal, vote in proposal_query.outerjoin(user_votes).with_entities(Proposal, user_votes).all():
        proposal.user_vote = vote
        if vote:
            if vote.state in ['new', 'resolved', 'stale']:
                proposal.is_new = True
                to_review_again.append(proposal)
            else:
                reviewed.append(((vote.state, vote.vote, vote.modified), proposal))
        else:
            # modified doesn't really describe when proposals are "new", but it's near enough
            if last_visit is None or review_order_dt is None or proposal.modified < review_order_dt:
                to_review_old.append(proposal)
            else:
                proposal.is_new = True
                to_review_new.append(proposal)

    reviewed = [p for o, p in sorted(reviewed, reverse=True)]

    review_order = session.get('review_order')
    if review_order is None \
           or not set([p.id for p in to_review_again]).issubset(review_order) \
           or (to_review_new and (last_visit is None or datetime.utcnow() - last_visit > timedelta(hours=1))):
        # For some reason random seems to 'stall' after the first run and stop
        # reshuffling the 'to_review' list. To force a reshuffle on each
        # execution we'll seed the RNG with the current time. This is pretty
        # horrible but at least works.
        # FIXME We shouldn't have to reseed the RNG every time
        random.seed(datetime.utcnow())

        random.shuffle(to_review_again)
        random.shuffle(to_review_new)
        random.shuffle(to_review_old)

        to_review_max = 30

        # prioritise showing proposals that have been voted on before
        # after that, split new and old proportionally for fairness
        to_review = to_review_again[:]
        other_max = max(0, to_review_max - len(to_review))
        other_count = len(to_review_old) + len(to_review_new)
        if other_count:
            old_max = int(float(len(to_review_old)) / other_count * other_max)
            new_max = other_max - old_max
            to_review += to_review_new[:new_max] + to_review_old[:old_max]

        session['review_order'] = [p.id for p in to_review]
        session['review_order_dt'] = last_visit
        session['review_visit_dt'] = datetime.utcnow()

    else:
        # Sort proposals based on the previous review order
        to_review_dict = dict((p.id, p) for p in to_review_again + to_review_new + to_review_old)
        to_review = [to_review_dict[i] for i in session['review_order'] if i in to_review_dict]

        session['review_visit_dt'] = datetime.utcnow()

    return render_template('cfp_review/review_list.html',
                           to_review=to_review, reviewed=reviewed, form=form)

class VoteForm(Form):
    vote_bad = SubmitField('Bad')
    vote_ok = SubmitField('OK')
    vote_excellent = SubmitField('Excellent')

    note = TextAreaField('Message')

    change = SubmitField("I'd like to change my response")
    recuse = SubmitField('I can identify the submitter (do not vote)')
    question = SubmitField('I need more information')

    def validate_note(form, field):
        if not field.data and form.recuse.data:
            raise ValidationError("Please tell us why you're not voting. If you can identify the submitter, please tell us who it is.")
        if not field.data and form.question.data:
            raise ValidationError("Please let us know what's unclear")


@cfp_review.route('/review/<int:proposal_id>', methods=['GET', 'POST'])
@review_required
def review_proposal(proposal_id):
    prop = Proposal.query.get(proposal_id)

    # Reviewers can only see anonymised proposals that aren't theirs
    # Also, only admin are reviewing installations
    if prop.state != 'anonymised'\
            or prop.user == current_user\
            or (prop.type == 'installation' and
                not current_user.has_permission('admin')):
        return abort(404)

    form = VoteForm()

    review_order = session.get('review_order')
    session['review_visit_dt'] = datetime.utcnow()

    # If the review order is missing redirect to the list to rebuild it
    if review_order is None:
        return redirect(url_for('.review_list'))

    if review_order and proposal_id in review_order:
        index = review_order.index(proposal_id) + 1
        next_id = review_order[index] if index < len(review_order) else None
        remaining = len(review_order)

    else:
        remaining = 0
        next_id = False

    vote = prop.get_user_vote(current_user)

    if form.validate_on_submit():
        # Make a new vote if need-be
        if not vote:
            vote = CFPVote(current_user, prop)
            db.session.add(vote)

        # If there's a note add it (will replace the old one but it's versioned)
        if form.note.data:
            vote.note = form.note.data
            vote.has_been_read = False
        else:
            vote.has_been_read = True

        vote_value = 2 if form.vote_excellent.data else\
                     1 if form.vote_ok.data else\
                     0 if form.vote_bad.data else None

        # Update vote state
        message = 'error'
        if vote_value is not None:
            vote.vote = vote_value
            vote.set_state('voted')
            review_order.remove(prop.id)
            message = 'You voted: ' + (['Bad', 'OK', 'Excellent'][vote_value])

        elif form.recuse.data:
            vote.set_state('recused')
            review_order.remove(prop.id)
            message = 'You declared a conflict of interest'

        elif form.question.data:
            vote.set_state('blocked')
            review_order.remove(prop.id)
            message = 'You requested more information'

        elif form.change.data:
            vote.set_state('resolved')
            message = 'Proposal re-opened for review'
            review_order.insert(0, proposal_id)
            next_id = proposal_id

        flash(message, 'info')
        session['review_order'] = review_order
        db.session.commit()
        if not next_id:
            return redirect(url_for('.review_list'))
        return redirect(url_for('.review_proposal', proposal_id=next_id))

    if vote and vote.note:
        form.note.data = vote.note
    return render_template('cfp_review/review_proposal.html',
                           form=form, proposal=prop, next_id=next_id,
                           previous_vote=vote, remaining=remaining)

class CloseRoundForm(Form):
    min_votes = IntegerField('Minimum number of votes', default=10, validators=[NumberRange(min=2)])
    close_round = SubmitField('Close this round')
    confirm = SubmitField('Confirm')
    cancel = SubmitField('Cancel')


@cfp_review.route('/close-round', methods=['GET', 'POST'])
@admin_required
def close_round():
    form = CloseRoundForm()
    min_votes = 0

    vote_subquery = CFPVote.query\
        .with_entities(
            CFPVote.proposal_id,
            func.count('*').label('count')
        )\
        .filter(CFPVote.state == 'voted')\
        .group_by('proposal_id')\
        .subquery()

    proposals = Proposal.query\
        .with_entities(Proposal, vote_subquery.c.count)\
        .join(
            vote_subquery,
            Proposal.id == vote_subquery.c.proposal_id
        )\
        .filter(
            Proposal.state.in_(['anonymised', 'reviewed'])
        ).order_by(vote_subquery.c.count.desc()).all()

    preview = False
    if form.validate_on_submit():
        if form.confirm.data:
            min_votes = session['min_votes']
            for (prop, vote_count) in proposals:
                if vote_count >= min_votes:
                    prop.set_state('reviewed')

            db.session.commit()
            del session['min_votes']
            app.logger.info("CFP Round closed. Set %d proposals to 'reviewed'" % len(proposals))

            return redirect(url_for('.rank'))

        elif form.close_round.data:
            preview = True
            session['min_votes'] = form.min_votes.data
            flash('Blue proposals will be marked as "reviewed"')

        elif form.cancel.data:
            form.min_votes.data = form.min_votes.default
            if 'min_votes' in session:
                del session['min_votes']

    return render_template('cfp_review/close-round.html', form=form,
                           proposals=proposals, preview=preview,
                           min_votes=session.get('min_votes'))

class AcceptanceForm(Form):
    min_score = FloatField('Minimum score for acceptance')
    set_score = SubmitField('Accept Proposals')
    confirm = SubmitField('Confirm')
    cancel = SubmitField('Cancel')


@cfp_review.route('/rank', methods=['GET', 'POST'])
@admin_required
def rank():
    proposals = Proposal.query\
        .filter_by(state='reviewed').all()

    form = AcceptanceForm()
    scored_proposals = []

    for prop in proposals:
        score_list = [v.vote for v in prop.votes if v.state == 'voted']
        score = calculate_max_normalised_score(score_list)
        scored_proposals.append((prop, score))

    scored_proposals = sorted(scored_proposals, key=lambda p: p[1], reverse=True)

    preview = False
    if form.validate_on_submit():
        if form.confirm.data:
            min_score = session['min_score']
            for (prop, score) in scored_proposals:
                count = 0

                if score >= min_score:
                    prop.set_state('accepted')
                    count += 1
                    send_template_email('Your EMF proposal has been accepted!',
                                        prop.user.email, app.config['CONTENT_EMAIL'],
                                        'cfp_review/email/accepted_msg.txt',
                                        user=prop.user, proposal=prop)

                elif not prop.has_rejected_email:
                    prop.has_rejected_email = True
                    send_template_email('Your EMF proposal',
                                        prop.user.email, app.config['CONTENT_EMAIL'],
                                        'cfp_review/email/not_accepted_msg.txt',
                                        user=prop.user, proposal=prop)

            db.session.commit()
            del session['min_score']
            msg = "Accepted %d proposals; min score: %d" % (count, min_score)
            app.logger.info(msg)
            flash(msg, 'info')
            return redirect(url_for('.proposals', state='accepted'))

        elif form.set_score.data:
            preview = True
            session['min_score'] = form.min_score.data
            flash('Blue proposals will be accepted', 'info')

        elif form.cancel.data and 'min_score' in session:
            del session['min_score']

    accepted_count = Proposal.query\
        .filter(
            Proposal.state.in_(['accepted', 'finished'])
        ).count()

    return render_template('cfp_review/rank.html', form=form, preview=preview,
                           proposals=scored_proposals, accepted_count=accepted_count,
                           min_score=session.get('min_score'))