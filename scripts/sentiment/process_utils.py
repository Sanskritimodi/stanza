import csv
import glob
import os

def get_scare_snippets(nlp, csv_dir_path, text_id_map):
    num_short_items = 0

    snippets = []
    csv_files = glob.glob(os.path.join(csv_dir_path, "*csv"))
    for csv_filename in csv_files:
        with open(csv_filename, newline='') as fin:
            cin = csv.reader(fin, delimiter='\t', quotechar='"')
            lines = list(cin)

            for line in lines:
                ann_id, begin, end, sentiment = [line[i] for i in [1, 2, 3, 6]]
                begin = int(begin)
                end = int(end)
                if sentiment == 'Unknown':
                    continue
                elif sentiment == 'Positive':
                    sentiment = 2
                elif sentiment == 'Neutral':
                    sentiment = 1
                elif sentiment == 'Negative':
                    sentiment = 0
                else:
                    raise ValueError("Tell John he screwed up and this is why he can't have Mox Opal")
                snippet = text_id_map[ann_id][begin:end]
                doc = nlp(snippet)
                text = " ".join(sentence.text for sentence in doc.sentences)
                num_tokens = sum(len(sentence.tokens) for sentence in doc.sentences)
                if num_tokens < 4:
                    num_short_items = num_short_items + 1
                snippets.append("%d %s" % (sentiment, text))
    print("Number of short items: {}".format(num_short_items))
    return snippets
